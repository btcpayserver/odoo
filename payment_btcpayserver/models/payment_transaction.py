import pprint

from odoo import _, api, fields, models

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.logging import get_payment_logger


_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    btcpay_invoiceId = fields.Char("Invoice Id")
    btcpay_txid = fields.Char("Transaction Id")
    btcpay_status = fields.Char("Transaction Status")
    api_url = '/btcpay/checkout'
    checkout_url = '/btcpay/checkout'
    notify_url = 'payment/btcpay/ipn'

    def _get_specific_rendering_values(self, processing_values):
        """ Override of payment to return BTCPay-specific rendering values.

        Note: self.ensure_one() from `_get_processing_values`

        :param dict processing_values: The generic and specific processing values of the transaction
        :return: The dict of provider-specific processing values
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)

        if self.provider_code != 'btcpayserver':
            return res

        base_url = self.provider_id.get_base_url()
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

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """ Override of payment to extract the transaction reference from BTCPay data.

        :param str provider_code: The code of the provider handling the transaction.
        :param dict payment_data: The payment data sent by the provider.
        :return: The transaction reference.
        :rtype: str
        """
        if provider_code != 'btcpayserver':
            return super()._extract_reference(provider_code, payment_data)

        return payment_data.get('reference')

    def _extract_amount_data(self, payment_data):
        """ Override of payment to skip amount validation for BTCPay.

        BTCPay invoices handle amount validation on the BTCPay server side,
        so we skip the Odoo-side validation.

        :param dict payment_data: The payment data sent by the provider.
        :return: None to skip validation.
        :rtype: None
        """
        if self.provider_code != 'btcpayserver':
            return super()._extract_amount_data(payment_data)

        return None

    def _apply_updates(self, payment_data):
        """ Override of payment to process the transaction based on BTCPay data.

        Note: self.ensure_one() from `_process`

        :param dict payment_data: The payment data sent by the provider.
        :return: None
        """
        if self.provider_code != 'btcpayserver':
            return super()._apply_updates(payment_data)

        _logger.info("BTCPay _apply_updates: %s", pprint.pformat(payment_data))

        self.provider_reference = payment_data.get('reference')
        self.btcpay_txid = payment_data.get('txid')
        self.btcpay_status = payment_data.get('status')

        if self.btcpay_status in ['paid', 'processing']:
            self._set_pending(state_message=payment_data.get('pending_reason'))
        elif self.btcpay_status in ['confirmed', 'complete']:
            self._set_done()
        elif self.btcpay_status in ['new']:
            self.btcpay_invoiceId = payment_data.get('invoiceID')
        elif self.btcpay_status in ['cancel', 'cancelled']:
            self._set_canceled()
        elif self.btcpay_status in ['invalid']:
            _logger.info(
                "Received data with invalid payment status (%s) for transaction with reference %s",
                self.btcpay_status, self.reference
            )
            self._set_error(
                "BTCPay: " + _("Received data with invalid payment status: %s", self.btcpay_status)
            )
