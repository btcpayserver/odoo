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

from openerp import api, fields, models, _
from openerp.osv import osv
from ..controller import crypto as bku
from ..controller.client import BTCPayClient

import logging
import pprint

from openerp import http, SUPERUSER_ID

_logger = logging.getLogger(__name__)

class AcquirerBtcPay(models.Model):
    _inherit = 'payment.acquirer'

    def _get_providers(self, cr, uid, context=None):
        providers = super(AcquirerBtcPay, self)._get_providers(cr, uid, context=context)
        providers.append(['btcpay', 'btcpay'])
        return providers

    token = fields.Char('Token', help='Access Token to BTCPay')
    privateKey = fields.Text('Private Key', help='Private Key for BTCPay Client')
    facade = fields.Char('Facade', help='Token facade type: merchant/pos/payroll')  #merchant
    pairingCode = fields.Char('Pairing Code', help='Create a paring Code in your BTCPay server and put here')
    location = fields.Char('Location', size=64) 
    confirmationURL = fields.Char('Confirmation URL', help='Confirmation URL to return after Btcpay payment')
    buyerNotification = fields.Boolean('Odoo confirmation mail to buyer', help='If it is checked, Odoo will send the confirmation mail defined')

    _defaults = {
        'facade': 'merchant',
        'location':'https://testnet.demo.btcpayserver.org', #Testnet BTCPay
        'confirmationURL':'http://odoo-dev.zynthian.org/shop/confirmation',
        'buyerNotification': 'True',
    }


    def create(self, cr, uid, values, context=None):
        if not values.get('privateKey'):
            values['privateKey'] = bku.generate_privkey()
        return super(AcquirerBtcPay, self).create(cr, uid, values, context=context)

    @api.onchange('pairingCode')
    def _onchange_pairingCode(self):
        if not self.token:
            client = BTCPayClient(host=self.location, pem=self.privateKey)
            token = client.pair_client(self.pairingCode)
            self.token = token.get(self.facade)

    @api.onchange('token')
    def _onchange_token(self):
        self.pairingCode = ''


class BtcPayTransaction(models.Model):
    _inherit = "payment.transaction"
    btcpay_invoiceId = fields.Char("Invoice Id")
    btcpay_txid = fields.Char("Transaction Id")
    btcpay_status = fields.Char("Transaction Status")
    btcpay_buyerMailNotification = fields.Char("Buyer Mail Notification")
    acquirer_name = fields.Selection(related='acquirer_id.provider')