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
import logging
import pprint

from werkzeug import urls

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ..models.libs.client import BTCPayClient

_logger = logging.getLogger(__name__)


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

    @http.route(_checkout_url, type='http', auth='public', methods=['POST'],
                csrf=False, website=True)
    def checkout(self, **data):
        """ Create the BTCPay invoice and redirect the buyer to it.

        The redirect form only carries the transaction reference. The invoice
        price, currency and buyer details are read from the transaction
        server-side and are never taken from the (client-controlled) request
        data, so a buyer cannot have an invoice created for a tampered amount.
        """
        _logger.info("BTCPay: checkout request with data:\n%s", pprint.pformat(data))
        tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
            'btcpayserver', data)
        if tx_sudo.state != 'draft':
            # Already processed (e.g. double submission): fall back to the
            # generic payment status page.
            return request.redirect('/payment/status')

        provider = tx_sudo.provider_id
        base_url = provider.get_base_url()
        client = self._btcpay_client(provider)
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
        tx_sudo.btcpay_invoiceId = invoice.get('id')
        _logger.info("BTCPay: created invoice %s for transaction %s",
                     invoice.get('id'), tx_sudo.reference)
        return request.redirect(invoice['url'], local=False)

    @http.route(_notify_url, type='json', auth='public', csrf=False)
    def btcpay_ipn(self, **post):
        """ BTCPay IPN. """
        _logger.info('BTCPay: IPN received')
        data = json.loads(request.httprequest.data)
        _logger.info("%s", pprint.pformat(data))
        try:
            notification_data = {
                "reference": data['data']['orderId'],
                "invoiceID": data['data']['id'],
            }
            # Check the origin and integrity of the notification: the invoice
            # is fetched back from BTCPay (signed request), the posted payload
            # is only used to locate the transaction.
            tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data(
                'btcpayserver', notification_data)
            client = self._btcpay_client(tx_sudo.provider_id)

            fetched_invoice = client.get_invoice(notification_data['invoiceID'])
            _logger.info('BTCPay: fetched invoice = %s', pprint.pformat(fetched_invoice))

            notification_data = {
                "reference": fetched_invoice.get('orderId'),
                "status": fetched_invoice.get('status'),
                "invoiceID": fetched_invoice.get('id'),
                "txid": fetched_invoice.get('url'),
                # Settled amount and currency, verified against the transaction
                # before it is set done.
                "amount": fetched_invoice.get('price'),
                "currency": fetched_invoice.get('currency'),
            }

            # Handle the notification data
            tx_sudo._handle_notification_data('btcpayserver', notification_data)
        except ValidationError:  # Acknowledge the notification to avoid getting spammed
            _logger.exception("Unable to handle the notification data; skipping to acknowledge")
        return ''

    @http.route(_return_url, type='http', auth="public", methods=['GET'],
                csrf=False, save_session=False)
    def btcpay_return_from_redirect(self, **data):
        """ BTCPay return

            We could process and check the invoice status here but there is no need to as the status get's updated via
            IPN anyway, so just show the user the order confirmation / payment status page.
        """
        _logger.info("BTCPay: user returned to shop after payment")
        return request.redirect('/payment/status')
