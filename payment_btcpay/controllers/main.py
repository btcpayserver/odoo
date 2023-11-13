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

import logging
import pprint

import requests
import werkzeug
from werkzeug import urls
from werkzeug.exceptions import Forbidden

from odoo import _, http, SUPERUSER_ID
from odoo.exceptions import ValidationError
from odoo.http import request
from odoo.tools import html_escape

import json
from ..models.libs.client import BTCPayClient


_logger = logging.getLogger(__name__)


class BTCPayController(http.Controller):
    _checkout_url = '/btcpay/checkout'
    _notify_url = 'payment/btcpay/ipn'

    @http.route(_checkout_url, type='http',  auth='public', csrf=False, webiste=True)
    def checkout(self, **data):

        _logger.info("CHECKOUT: notification received from BTCPay with data:\n%s", pprint.pformat(data))
        tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data('btcpay', data)
        provider = tx_sudo.provider_id
        notificationURL = str(data.get('notify_url')).replace("http://", "https://")
        client = BTCPayClient(host=provider.btcpay_location, pem=provider.btcpay_privateKey, tokens={provider.btcpay_facade: provider.btcpay_token})
        invoice = client.create_invoice(
            {"price": data.get('amount'),
             "currency": data.get('currency_id'),
             "orderId": data.get('reference'),
             "token": provider.btcpay_token,
             "redirectURL": provider.btcpay_confirmationURL,
             "notificationURL": notificationURL,
             "extendedNotifications": True,
             "buyer": {"email": data.get('email'),
                       "name": data.get('name'),
                       "address1": data.get('street'),
                       "locality": data.get('city'),
                       "postalCode": data.get('zip'),
                       "country": data.get('country'),
                       "notify": False}})
        _logger.info('Invoice %s \n NOTIFY URL: %s', invoice, notificationURL)
        return werkzeug.utils.redirect(invoice['url'])

    @http.route('/payment/btcpay/ipn', type='json', auth='public', csrf=False)
    def btcpay_ipn(self, **post):
        """ BTCPay IPN. """
        _logger.info('BTCPAY IPN RECEIVED... ')
        data = json.loads(request.httprequest.data)
        _logger.info("%s", pprint.pformat(data))
        try:
            notification_data = {"reference": data['data']['orderId'],
                                 "invoiceID": data['data']['id']}
            # Check the origin and integrity of the notification
            tx_sudo = request.env['payment.transaction'].sudo()._get_tx_from_notification_data('btcpay', notification_data)
            provider = tx_sudo.provider_id
            client = BTCPayClient(host=provider.btcpay_location, pem=provider.btcpay_privateKey,
                                  tokens={provider.btcpay_facade: provider.btcpay_token})

            fetched_invoice = client.get_invoice(notification_data['invoiceID'])
            _logger.info('fetched_invoice = %s',pprint.pformat(fetched_invoice))

            notification_data = {"reference": fetched_invoice['orderId'],
                    "status": fetched_invoice['status'],
                    "invoiceID": fetched_invoice['id'],
                    "txid": fetched_invoice['url']}

            # Handle the notification data
            tx_sudo._handle_notification_data('btcpay', notification_data)
        except ValidationError:  # Acknowledge the notification to avoid getting spammed
            _logger.exception("unable to handle the notification data; skipping to acknowledge")
        return ''
