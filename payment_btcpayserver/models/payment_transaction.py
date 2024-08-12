import logging
import pprint
from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.payment import utils as payment_utils

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    btcpay_invoiceId = fields.Char("Invoice Id")
    btcpay_txid = fields.Char("Transaction Id")
    btcpay_status = fields.Char("Transaction Status")
    api_url = '/btcpay/checkout'
    checkout_url = '/btcpay/checkout'
    notify_url = 'payment/btcpay/ipn'

    def _get_specific_rendering_values(self, processing_values):
        """ Override of payment to return Specific rendering values.

        Note: self.ensure_one() from `_get_processing_values`

        :param dict processing_values: The generic and specific processing values of the transaction
        :return: The dict of provider-specific processing values
        :rtype: dict
        """

        res = super()._get_specific_rendering_values(processing_values)

        if self.provider_code != 'btcpayserver':
            return res

        base_url = self.provider_id.get_base_url()
        _logger.info('Hola! API URL: %s', processing_values)
        partner_first_name, partner_last_name = payment_utils.split_partner_name(self.partner_name)

        return {
            'address1': self.partner_address,
            'amount': self.amount,
            'city': self.partner_city,
            'country': self.partner_country_id.code,
            'currency_code': self.currency_id.name,
            'email': self.partner_email,
            'first_name': partner_first_name,
            'item_name': f"{self.company_id.name}: {self.reference}",
            'item_number': self.reference,
            'last_name': partner_last_name,
            'lc': self.partner_lang,
            'state': self.partner_state_id.name,
            'zip_code': self.partner_zip,
            'api_url':  self.checkout_url,
            'notify_url': base_url + self.notify_url,
        }

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        """ Override of payment to find the transaction based on BTCPay data.

        :param str provider_code: The code of the provider that handled the transaction
        :param dict notification_data: The notification data sent by the provider
        :return: The transaction if found
        :rtype: recordset of `payment.transaction`
        :raise: ValidationError if the data match no transaction
        """
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        _logger.info('GET TX FROM NOTIFICATION Notification_data %s', pprint.pformat(notification_data))
        if provider_code != 'btcpayserver' or len(tx) == 1:
            return tx

        reference = notification_data.get('reference')
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'btcpayserver')])
        if not tx:
            raise ValidationError(
                "BTCPay: " + _("No transaction found matching reference %s.", reference)
            )
        return tx

    def _handle_notification_data(self, provider_code, notification_data):
        """ Match the transaction with the notification data, update its state and return it.

        :param str provider_code: The code of the provider handling the transaction.
        :param dict notification_data: The notification data sent by the provider.
        :return: The transaction.
        :rtype: recordset of `payment.transaction`
        """
        tx = self._get_tx_from_notification_data(provider_code, notification_data)
        tx._process_notification_data(notification_data)
        tx._execute_callback()
        return tx

    def _process_notification_data(self, notification_data):
        """ Override of payment to process the transaction based on BTCPay data.

        Note: self.ensure_one()

        :param dict notification_data: The notification data sent by the provider
        :return: None
        :raise: ValidationError if inconsistent data were received
        """
        super()._process_notification_data(notification_data)
        if self.provider_code != 'btcpayserver':
            return

        _logger.info("_process_notification_data %s", pprint.pformat(notification_data))
        txn_id = notification_data.get('reference')
        if not all(txn_id):
            raise ValidationError(
                "BTCPay: " + _("Missing value for txn_id (%(txn_id)s)).", txn_id=txn_id))

        self.provider_reference = txn_id
        self.btcpay_txid = notification_data.get('txid')
        self.btcpay_status = notification_data.get('status')

        if self.btcpay_status in ['paid','processing']:
            self._set_pending(state_message=notification_data.get('pending_reason'))
        elif self.btcpay_status in ['confirmed', 'complete']:
            self._set_done()
            confirmed_orders = self._check_amount_and_confirm_order()
            confirmed_orders._send_order_confirmation_mail()
        elif self.btcpay_status in ['new']:
            self.btcpay_invoiceId = notification_data.get('invoiceID')
        elif self.btcpay_status in ['cancel','cancelled']:
            self._set_canceled()
        elif self.btcpay_status in ['invalid']:
            _logger.info(
                "received data with invalid payment status (%s) for transaction with reference %s",
                self.btcpay_status, self.reference
            )
            self._set_error(
                "BTCPay: " + _("Received data with invalid payment status: %s", self.btcpay_status)
            )