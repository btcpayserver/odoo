# ******************************************************************************
# PAYMENT BTCPAY FOR ODOO
#
# Copyright (C) 2020 Susanna Fort <susannafm@gmail.com>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of
# the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

import json

from werkzeug import urls

from odoo import _, http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.payment.logging import get_payment_logger
from ..models.libs.client import BTCPayClient


_logger = get_payment_logger(__name__)


class BTCPayController(http.Controller):
    _checkout_url = '/btcpay/checkout'
    _notify_url = '/payment/btcpay/ipn'
    _return_url = '/payment/btcpay/return'

    @staticmethod
    def _btcpay_client(provider):
        return BTCPayClient(
            host=provider.btcpay_location,
            pem=provider.btcpay_privateKey,
            tokens={provider.btcpay_facade: provider.btcpay_token},
        )

    @staticmethod
    def _find_transaction(reference):
        return request.env['payment.transaction'].sudo().search([
            ('reference', '=', reference),
            ('provider_code', '=', 'btcpayserver'),
        ], limit=1)

    @staticmethod
    def _lock_transaction(tx_sudo):
        """Lock a transaction until the current database transaction ends.

        Checkout and IPN requests can arrive concurrently. Serializing them on
        the payment transaction prevents two checkout requests from creating
        separate invoices and makes an IPN re-check the invoice ID after a
        checkout replaces an expired invoice.
        """
        tx_sudo.ensure_one()
        tx_sudo.flush_recordset()
        tx_sudo.env.cr.execute(
            "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE",
            [tx_sudo.id],
        )
        tx_sudo.invalidate_recordset([
            'state', 'btcpay_invoiceId', 'provider_reference',
        ])

    @staticmethod
    def _ensure_invoice_id_is_unique(tx_sudo, invoice_id):
        """Reject an invoice ID already linked to another transaction."""
        other_tx = tx_sudo.env['payment.transaction'].sudo().search([
            ('id', '!=', tx_sudo.id),
            ('provider_code', '=', 'btcpayserver'),
            '|',
            ('btcpay_invoiceId', '=', invoice_id),
            ('provider_reference', '=', invoice_id),
        ], limit=1)
        if other_tx:
            raise ValidationError(_(
                "BTCPay: The invoice is already linked to another transaction."
            ))

    @classmethod
    def _validate_invoice_binding(
            cls, tx_sudo, incoming_invoice_id, incoming_order_id, fetched_invoice):
        """Bind the untrusted IPN payload to one authenticated invoice and transaction."""
        if not isinstance(fetched_invoice, dict):
            raise ValidationError(_("BTCPay: Received an invalid invoice response."))

        fetched_invoice_id = fetched_invoice.get('id')
        fetched_order_id = fetched_invoice.get('orderId')
        if not (
            incoming_invoice_id
            and incoming_invoice_id == fetched_invoice_id
            and incoming_invoice_id == tx_sudo.btcpay_invoiceId
            and incoming_invoice_id == tx_sudo.provider_reference
        ):
            raise ValidationError(_(
                "BTCPay: The notification invoice does not match the transaction."
            ))
        if not (
            incoming_order_id
            and incoming_order_id == fetched_order_id
            and incoming_order_id == tx_sudo.reference
        ):
            raise ValidationError(_(
                "BTCPay: The notification order does not match the transaction."
            ))
        cls._ensure_invoice_id_is_unique(tx_sudo, incoming_invoice_id)

    @classmethod
    def _find_transaction_by_invoice_id(cls, invoice_id):
        """Find exactly one BTCPay transaction from its stored invoice ID."""
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValidationError(_("BTCPay: Missing invoice ID in notification data."))

        txs_sudo = request.env['payment.transaction'].sudo().search([
            ('provider_code', '=', 'btcpayserver'),
            ('btcpay_invoiceId', '=', invoice_id),
        ], limit=2)
        if len(txs_sudo) != 1:
            raise ValidationError(_(
                "BTCPay: No unique transaction found matching the invoice ID."
            ))
        return txs_sudo

    @staticmethod
    def _validate_invoice_amount(tx_sudo, invoice):
        """Reject reuse of an invoice with a different amount or currency."""
        try:
            amount = float(invoice.get('price'))
        except (AttributeError, TypeError, ValueError):
            amount = None
        if (
            amount is None
            or invoice.get('currency') != tx_sudo.currency_id.name
            or tx_sudo.currency_id.compare_amounts(amount, tx_sudo.amount) != 0
        ):
            raise ValidationError(_(
                "BTCPay: The existing invoice amount or currency does not match the transaction."
            ))

    @classmethod
    def _validate_existing_invoice(cls, tx_sudo, invoice_id, invoice):
        """Validate a stored invoice before redirecting the customer to it."""
        cls._validate_invoice_binding(
            tx_sudo, invoice_id, tx_sudo.reference, invoice,
        )
        if not isinstance(invoice.get('status'), str) or not invoice['status']:
            raise ValidationError(_("BTCPay: The existing invoice response is incomplete."))

    @classmethod
    def _validate_created_invoice(cls, tx_sudo, invoice):
        """Validate a newly created invoice before storing its identity."""
        if not isinstance(invoice, dict):
            raise ValidationError(_("BTCPay: Received an invalid invoice response."))
        invoice_id = invoice.get('id')
        if (
            not isinstance(invoice_id, str)
            or not invoice_id
            or not isinstance(invoice.get('url'), str)
            or not invoice['url']
            or invoice.get('orderId') not in (None, tx_sudo.reference)
        ):
            raise ValidationError(_("BTCPay: The created invoice response is incomplete."))
        cls._ensure_invoice_id_is_unique(tx_sudo, invoice_id)

    @http.route(_checkout_url, type='http', auth='public', methods=['POST'],
                csrf=False, website=True)
    def checkout(self, **data):
        """ Create the BTCPay invoice and redirect the buyer to it.

        The redirect form only carries the transaction reference. The invoice
        price, currency and buyer details are read from the transaction
        server-side and are never taken from the (client-controlled) request
        data, so a buyer cannot have an invoice created for a tampered amount.
        """
        _logger.info("BTCPay: checkout request for transaction %s", data.get('reference'))
        reference = data.get('reference')
        tx_sudo = self._find_transaction(reference)
        if not tx_sudo:
            raise ValidationError(
                _("BTCPay: No transaction found matching reference %s.", reference))
        self._lock_transaction(tx_sudo)
        if tx_sudo.state != 'draft':
            # Already processed (e.g. double submission): fall back to the
            # generic payment status page.
            return request.redirect('/payment/status')

        provider = tx_sudo.provider_id
        base_url = provider.get_base_url()
        client = self._btcpay_client(provider)

        # A browser retry must not create a second payable invoice. Reuse the
        # stored invoice while it remains payable. If it expired, the new ID is
        # stored before the lock is released, so late notifications for the old
        # invoice can no longer select this transaction.
        if tx_sudo.btcpay_invoiceId:
            existing_invoice = client.get_invoice(tx_sudo.btcpay_invoiceId)
            self._validate_existing_invoice(
                tx_sudo, tx_sudo.btcpay_invoiceId, existing_invoice,
            )
            if existing_invoice['status'].lower() != 'expired':
                self._validate_invoice_amount(tx_sudo, existing_invoice)
                if (
                    not isinstance(existing_invoice.get('url'), str)
                    or not existing_invoice['url']
                ):
                    raise ValidationError(_(
                        "BTCPay: The existing invoice response is incomplete."
                    ))
                _logger.info(
                    "BTCPay: reusing invoice %s for transaction %s",
                    tx_sudo.btcpay_invoiceId, tx_sudo.reference,
                )
                return request.redirect(existing_invoice['url'], local=False)

        invoice = client.create_invoice({
            "price": tx_sudo.amount,
            "currency": tx_sudo.currency_id.name,
            "orderId": tx_sudo.reference,
            "token": provider.btcpay_token,
            "redirectURL": urls.url_join(base_url, self._return_url),
            "notificationURL": urls.url_join(base_url, self._notify_url),
            "extendedNotifications": True,
            "buyer": {
                "email": tx_sudo.partner_email or 'noemailavailable@example.com',
                "name": tx_sudo.partner_name,
                "address1": tx_sudo.partner_address,
                "locality": tx_sudo.partner_city,
                "postalCode": tx_sudo.partner_zip,
                "country": tx_sudo.partner_country_id.code,
                "notify": False,
            },
        })
        self._validate_created_invoice(tx_sudo, invoice)
        tx_sudo.write({
            'btcpay_invoiceId': invoice['id'],
            'provider_reference': invoice['id'],
        })
        _logger.info("BTCPay: created invoice %s for transaction %s",
                     invoice['id'], tx_sudo.reference)
        return request.redirect(invoice['url'], local=False)

    @http.route(_notify_url, type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def btcpay_ipn(self, **post):
        """ BTCPay IPN. """
        _logger.info('BTCPay: IPN received')
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _logger.warning("BTCPay: received malformed JSON notification data")
            return ''

        try:
            ipn_data = data.get('data') if isinstance(data, dict) else None
            if not isinstance(ipn_data, dict):
                raise ValidationError(_("BTCPay: Invalid notification data."))
            incoming_invoice_id = ipn_data.get('id')
            incoming_order_id = ipn_data.get('orderId')

            # Never use the untrusted order ID to select a transaction. The
            # stored invoice ID is the lookup key.
            tx_sudo = self._find_transaction_by_invoice_id(incoming_invoice_id)
            client = self._btcpay_client(tx_sudo.provider_id)

            # Fetching through the authenticated API establishes the invoice's
            # current state. Lock afterwards and re-check the stored ID so a
            # concurrent checkout cannot replace it between lookup and use.
            fetched_invoice = client.get_invoice(incoming_invoice_id)
            self._lock_transaction(tx_sudo)
            self._validate_invoice_binding(
                tx_sudo, incoming_invoice_id, incoming_order_id, fetched_invoice,
            )
            _logger.info(
                "BTCPay: verified invoice %s with status %s for transaction %s",
                fetched_invoice.get('id'), fetched_invoice.get('status'), tx_sudo.reference,
            )

            payment_data = {
                "reference": tx_sudo.reference,
                "status": fetched_invoice.get('status'),
                "invoiceID": incoming_invoice_id,
                "txid": fetched_invoice.get('url'),
                # Settled amount and currency, validated against the transaction
                # by the payment framework before it is set done.
                "amount": fetched_invoice.get('price'),
                "currency": fetched_invoice.get('currency'),
            }

            tx_sudo._process('btcpayserver', payment_data)
        except ValidationError as error:
            _logger.warning("BTCPay: rejected notification: %s", error)
        return ''

    @http.route(_return_url, type='http', auth="public", methods=['GET'],
                csrf=False, save_session=False)
    def btcpay_return_from_redirect(self, **data):
        """ BTCPay return """
        _logger.info("BTCPay: user returned to shop after payment")
        return request.redirect('/payment/status')
