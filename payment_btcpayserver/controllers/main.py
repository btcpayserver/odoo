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
from ..models.libs.greenfield_client import GreenfieldClient


_logger = get_payment_logger(__name__)


class BTCPayController(http.Controller):
    _checkout_url = '/btcpay/checkout'
    _return_url = '/payment/btcpay/return'

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
        """Bind the untrusted webhook payload to one authenticated invoice and transaction."""
        if not isinstance(fetched_invoice, dict):
            raise ValidationError(_("BTCPay: Received an invalid invoice response."))

        fetched_invoice_id = fetched_invoice.get('id')
        fetched_order_id = cls._get_invoice_order_id(fetched_invoice)
        if not (
            incoming_invoice_id
            and incoming_invoice_id == fetched_invoice_id
            and incoming_invoice_id == tx_sudo.btcpay_invoiceId
            and incoming_invoice_id == tx_sudo.provider_reference
        ):
            raise ValidationError(_(
                "BTCPay: The notification invoice does not match the transaction."
            ))
        # Greenfield stores the order reference in metadata.orderId (legacy
        # invoices fetched through Greenfield keep a top-level orderId). The
        # fetched invoice must belong to the transaction's reference; if the
        # payload itself carried an order id it must match as well.
        if not fetched_order_id or fetched_order_id != tx_sudo.reference:
            raise ValidationError(_(
                "BTCPay: The notification order does not match the transaction."
            ))
        if incoming_order_id and incoming_order_id != tx_sudo.reference:
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
    def _get_invoice_order_id(invoice):
        """Return the order reference stored on the invoice.

        Greenfield keeps it in ``metadata.orderId``; invoices created through
        the legacy API expose a top-level ``orderId`` when fetched via
        Greenfield.
        """
        if not isinstance(invoice, dict):
            return None
        metadata = invoice.get('metadata') or {}
        return metadata.get('orderId') or invoice.get('orderId')

    @staticmethod
    def _validate_invoice_amount(tx_sudo, invoice):
        """Reject reuse of an invoice with a different amount or currency."""
        try:
            amount = float(invoice.get('amount'))
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
            or not isinstance(invoice.get('checkoutLink'), str)
            or not invoice['checkoutLink']
            or cls._get_invoice_order_id(invoice) not in (None, tx_sudo.reference)
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
        client = provider._btcpay_client()

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
                    not isinstance(existing_invoice.get('checkoutLink'), str)
                    or not existing_invoice['checkoutLink']
                ):
                    raise ValidationError(_(
                        "BTCPay: The existing invoice response is incomplete."
                    ))
                _logger.info(
                    "BTCPay: reusing invoice %s for transaction %s",
                    tx_sudo.btcpay_invoiceId, tx_sudo.reference,
                )
                return request.redirect(existing_invoice['checkoutLink'], local=False)

        invoice = client.create_invoice(
            amount=tx_sudo.amount,
            currency=tx_sudo.currency_id.name,
            metadata={'orderId': tx_sudo.reference},
            checkout={
                'redirectURL': urls.url_join(base_url, self._return_url),
                'redirectAutomatically': True,
            },
        )
        self._validate_created_invoice(tx_sudo, invoice)
        # Since Odoo 20.0, payment.transaction.write() requires the
        # payment_safe_write context flag. Storing the freshly created invoice
        # id is a preparation write: if the request rolls back, the next
        # checkout simply creates a new invoice (the unbound one expires).
        tx_sudo.with_context(payment_safe_write=True).write({
            'btcpay_invoiceId': invoice['id'],
            'provider_reference': invoice['id'],
        })
        _logger.info("BTCPay: created invoice %s for transaction %s",
                     invoice['id'], tx_sudo.reference)
        return request.redirect(invoice['checkoutLink'], local=False)

    @http.route('/payment/btcpay/webhook', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def btcpay_webhook(self, **post):
        """ BTCPay Greenfield webhook (store-level, HMAC-signed). """
        _logger.info('BTCPay: webhook received')
        raw_body = request.httprequest.get_data()
        signature = request.httprequest.headers.get('BTCPay-Sig')
        try:
            data = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _logger.warning("BTCPay: received malformed JSON webhook data")
            return request.make_response('malformed JSON', status=400)
        if not isinstance(data, dict):
            _logger.warning("BTCPay: received non-object webhook data")
            return request.make_response('invalid payload', status=400)

        # The store id selects the provider (and thus the secret); signature
        # verification then authenticates the payload itself.
        provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'btcpayserver'),
            ('btcpay_store_id', '=', data.get('storeId') or ''),
        ], limit=1)
        if not provider or not provider.btcpay_webhook_secret:
            _logger.warning(
                "BTCPay: webhook for unknown or unconnected store %s",
                data.get('storeId'),
            )
            return request.make_response('unknown store', status=400)
        if not GreenfieldClient.is_valid_webhook_signature(
                raw_body, signature, provider.btcpay_webhook_secret):
            _logger.warning("BTCPay: invalid webhook signature, rejecting")
            return request.make_response('invalid signature', status=400)

        event_type = data.get('type') or ''
        incoming_invoice_id = data.get('invoiceId')
        if not incoming_invoice_id:
            # Events without an invoice (e.g. webhook test notifications).
            _logger.info("BTCPay: ignored webhook event %s (no invoice)", event_type)
            return ''
        if data.get('isRedelivery'):
            _logger.info("BTCPay: webhook %s is a redelivery", event_type)

        try:
            # Never trust payload data to select the transaction: the stored
            # invoice ID is the lookup key.
            tx_sudo = self._find_transaction_by_invoice_id(incoming_invoice_id)

            # Fetching through the authenticated API establishes the invoice's
            # current state. Lock afterwards and re-check the stored ID so a
            # concurrent checkout cannot replace it between lookup and use.
            fetched_invoice = provider._btcpay_client().get_invoice(incoming_invoice_id)
            self._lock_transaction(tx_sudo)
            self._validate_invoice_binding(
                tx_sudo, incoming_invoice_id, data.get('orderId'), fetched_invoice,
            )
            _logger.info(
                "BTCPay: verified invoice %s with status %s/%s for transaction %s (event %s)",
                fetched_invoice.get('id'), fetched_invoice.get('status'),
                fetched_invoice.get('additionalStatus'), tx_sudo.reference, event_type,
            )

            payment_data = {
                "reference": tx_sudo.reference,
                "status": fetched_invoice.get('status'),
                "additionalStatus": fetched_invoice.get('additionalStatus'),
                "invoiceID": incoming_invoice_id,
                "txid": fetched_invoice.get('checkoutLink'),
                # Settled amount and currency, validated against the transaction
                # by the payment framework before it is set done.
                "amount": fetched_invoice.get('amount'),
                "currency": fetched_invoice.get('currency'),
            }

            # Since Odoo 20.0, _process() takes no provider code anymore and
            # payment data must go through _record(): it is stored as a
            # payment.data record and applied by the processing cron with
            # proper locking. The /payment/status page triggers immediate
            # processing when the customer is redirected back to the shop.
            tx_sudo._record(payment_data)
        except ValidationError as error:
            _logger.warning("BTCPay: rejected webhook: %s", error)
        return ''

    @http.route(_return_url, type='http', auth="public", methods=['GET'],
                csrf=False, save_session=False)
    def btcpay_return_from_redirect(self, **data):
        """ BTCPay return """
        _logger.info("BTCPay: user returned to shop after payment")
        return request.redirect('/payment/status')
