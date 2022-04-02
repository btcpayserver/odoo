#******************************************************************************
# PAYMENT BTCPAY FOR ODOO
# 
# Copyright (C) 2020 Susanna Fort <susannafm@gmail.com>
#
#******************************************************************************
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
#******************************************************************************

import json
import logging
import pprint

import requests
import werkzeug

import client
import crypto as bku
from client import BTCPayClient
import urllib2,cookielib

from openerp import api, fields, models, _
from openerp.osv import osv

from openerp import http, SUPERUSER_ID
from openerp.addons.payment.models.payment_acquirer import ValidationError
from openerp.http import request


_logger = logging.getLogger(__name__)


class BtcpayController(http.Controller):
    _notify_url = '/payment/btcpay/ipn'

    @http.route('/payment/btcpay/ipn', type='json', auth='public', methods=['POST'], csrf=False)
    def btcpay_ipn(self, **post):
        """ BTCPay IPN. """
        _logger.info('BTCPAY IPN RECEIVED...')
        cr, uid, context, env = request.cr, SUPERUSER_ID, request.context, request.env
        acquirer = env['payment.acquirer'].search([('provider', '=', 'btcpay')])
        try:
            #for k in request.jsonrequest['data']:
            #    _logger.info('\t%s => %s', k, request.jsonrequest['data'][k])
            invoiceId = request.jsonrequest['data']['id']
            invoiceStatus = request.jsonrequest['data']['status']
            orderId = request.jsonrequest['data']['orderId']
            _logger.info('Invoice ID %s => %s (Order ID %s)', invoiceId, invoiceStatus, orderId)

            #client = BTCPayClient(host=acquirer.location, pem=acquirer.privateKey, tokens=acquirer.token)                
            #self.invoice = client.get_invoice(invoiceId)
            #_logger.info('SELF INVOICE IPN %s',self.invoice)
       
            if invoiceId and orderId:
                tx_ids = request.registry['payment.transaction'].search(cr, uid, [('reference', '=', orderId)], context=context)
                if tx_ids:
                    tx = request.registry['payment.transaction'].browse(cr, uid, tx_ids[0], context=context)

                    tx.btcpay_status = invoiceStatus
                    if invoiceStatus in ['confirmed']:
                        tx.state = 'done'
                        tx.sale_order_id.state = 'sale'
                        if not tx.btcpay_buyerMailNotification and acquirer.buyerNotification:
                            tx.sale_order_id.force_quotation_send()
                            tx.btcpay_buyerMailNotification = "Send"
                            tx.sale_order_id.order_line._action_procurement_create()
                    elif invoiceStatus in ['paid']:
                        tx.state = 'pending'
                        tx.btcpay_invoiceId = invoiceId
                        tx.btcpay_txid = request.jsonrequest['data']['url']

        except Exception as e:
            _logger.error(e)
        return ''


    @http.route(['/btcpay/checkout'], type='http', auth='none', csrf=None, website=True)
    def checkout(self, **post):
        cr, uid, context, env = request.cr, SUPERUSER_ID, request.context, request.env
        acquirer = env['payment.acquirer'].search([('provider', '=', 'btcpay')])
        currency = env['res.currency'].browse(eval(post.get('currency_id'))).name
        base_url = request.env['ir.config_parameter'].get_param('web.base.url')
        return_url = base_url + self._notify_url
       
        client = BTCPayClient(host=acquirer.location, pem=acquirer.privateKey, tokens=acquirer.token)
        
        acquirer.invoice = client.create_invoice(
            {"price": post.get('amount'),
            "currency": currency,
            "orderId": post.get('reference'),
            "token": acquirer.token,
            "redirectURL": acquirer.confirmationURL,
            "notificationURL": return_url,
            "extendedNotifications": True,
            "buyer": {  "email": post.get('email'),
                        "name": post.get('name'),
                        "address1": post.get('street'),
                        "locality": post.get('city'),
                        "postalCode": post.get('zip'),
                        "country": post.get('country'),
                        "notify": False}})
        invoiceId = dict(acquirer.invoice)['id']
        self.invoice = client.get_invoice(invoiceId)
        return werkzeug.utils.redirect(self.invoice['url'])
    