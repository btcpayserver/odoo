#******************************************************************************
# PAYMENT BTCPAY FOR ODOO
#
# Copyright (C) 2023 Susanna Fort <susannafm@gmail.com>
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
    'name': 'Payment Provider: BTCPay',
    'summary': 'This module integrates BTCPAY - pay with Bitcoin - with Odoo v16.0',
    'author': 'Vandekul',
    'website': 'https://github.com/btcpayserver/odoo',
    'category': 'Accounting/Payment Providers',
    'version': '16.0',
    'license': 'GPL-3',
    'application': False,
    'installable': True,
    'auto_install': False,
    'depends': ['base', 'account', 'payment'],
    'data': [
        'views/payment_btcpay_templates.xml',
        'views/payment_provider_views.xml',
        'views/payment_transaction_views.xml',
        'data/payment_provider_data.xml',
    ],
    'external_dependencies': {
        'python': ['btcpay-python']
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
}