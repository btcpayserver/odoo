import pprint

from odoo import _, api, fields, models

from odoo.addons.payment.logging import get_payment_logger


_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    btcpay_invoiceId = fields.Char("Invoice Id")
    btcpay_txid = fields.Char("Transaction Id")
    btcpay_status = fields.Char("Transaction Status")

    def _get_specific_rendering_values(self, processing_values):
        """ Override of payment to return BTCPay-specific rendering values.

        The redirect form only needs to carry the transaction reference: the
        BTCPay invoice is built server-side (see the checkout controller) from
        the transaction, so no amount, currency or buyer data is sent through
        the browser.

        Note: self.ensure_one() from `_get_processing_values`

        :param dict processing_values: The generic and specific processing values of the transaction
        :return: The dict of provider-specific processing values
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'btcpayserver':
            return res

        return {
            'api_url': '/btcpay/checkout',
            'reference': self.reference,
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
        """ Override of payment to return the settled amount and currency.

        The amount and currency are read back from BTCPay on notification and
        returned here so that the generic amount validation compares them
        against the transaction. This prevents a tampered or mismatched invoice
        from confirming the transaction (and thus the order).

        :param dict payment_data: The payment data sent by the provider.
        :return: The settled amount data, or ``None`` for other providers.
        :rtype: dict|None
        """
        if self.provider_code != 'btcpayserver':
            return super()._extract_amount_data(payment_data)

        try:
            amount = float(payment_data.get('amount'))
        except (TypeError, ValueError):
            amount = None
        return {
            'amount': amount,
            'currency_code': payment_data.get('currency'),
        }

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
        elif self.btcpay_status in ['expired', 'cancel', 'cancelled']:
            self._set_canceled(
                state_message="BTCPay: " + _("Invoice status: %s.", self.btcpay_status))
        elif self.btcpay_status in ['invalid']:
            _logger.warning(
                "Received data with invalid payment status (%s) for transaction with reference %s",
                self.btcpay_status, self.reference
            )
            self._set_error(
                "BTCPay: " + _("Received data with invalid payment status: %s.", self.btcpay_status)
            )
        else:
            _logger.warning(
                "Received data with unknown payment status (%s) for transaction with reference %s",
                self.btcpay_status, self.reference
            )
            self._set_error(
                "BTCPay: " + _("Received data with unknown payment status: %s.", self.btcpay_status)
            )
