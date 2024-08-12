#******************************************************************************
# PAYMENT BTCPAY SERVER FOR ODOO
#
# Copyright (C) 2024 Susanna Fort <susannafm@gmail.com>, ndeet
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

{
    'name': 'Payment Provider: BTCPay Server',
    'summary': 'This module integrates BTCPAY - pay with Bitcoin - with Odoo v17.0',
    'author': 'BTCPay Server team and contributors',
    'website': 'https://github.com/btcpayserver/odoo',
    'category': 'Accounting/Payment Providers',
    'version': '17.0.2.0',
    'license': 'GPL-3',
    'currency': 'USD',
    'application': False,
    'installable': True,
    'auto_install': False,
    'depends': ['base', 'account', 'payment'],
    'data': [
        'views/payment_btcpayserver_templates.xml',
        'views/payment_provider_views.xml',
        'views/payment_transaction_views.xml',
        'data/payment_provider_data.xml',
    ],
    'images':  ['static/description/BTCPay-Odoo-17-featured.png'],
    'external_dependencies': {
        'python': ['btcpay-python']
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
}