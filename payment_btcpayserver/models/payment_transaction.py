import logging
import pprint

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


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

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        """ Override of payment to find the transaction based on BTCPay data.

        :param str provider_code: The code of the provider that handled the transaction
        :param dict notification_data: The notification data sent by the provider
        :return: The transaction if found
        :rtype: recordset of `payment.transaction`
        :raise: ValidationError if the data match no transaction
        """
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'btcpayserver' or len(tx) == 1:
            return tx

        reference = notification_data.get('reference')
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'btcpayserver')])
        if not tx:
            raise ValidationError(
                "BTCPay: " + _("No transaction found matching reference %s.", reference)
            )
        return tx

    def _process_notification_data(self, notification_data):
        """ Override of payment to process the transaction based on BTCPay data.

        Only the transaction state is updated here; confirming the related
        sale order, creating the payment and sending the confirmation email are
        handled by the generic post-processing of the `payment` framework.

        Note: self.ensure_one()

        :param dict notification_data: The notification data sent by the provider
        :return: None
        :raise: ValidationError if inconsistent data were received
        """
        super()._process_notification_data(notification_data)
        if self.provider_code != 'btcpayserver':
            return

        _logger.info(
            "processing notification data for BTCPay transaction with reference %s:\n%s",
            self.reference, pprint.pformat(notification_data))
        if not notification_data.get('reference'):
            raise ValidationError(
                "BTCPay: " + _("Received notification data with missing reference."))

        if notification_data.get('invoiceID'):
            self.btcpay_invoiceId = notification_data['invoiceID']
            self.provider_reference = notification_data['invoiceID']
        if notification_data.get('txid'):
            self.btcpay_txid = notification_data['txid']
        status = notification_data.get('status')
        self.btcpay_status = status

        if status in ('paid', 'processing'):
            self._set_pending(state_message=notification_data.get('pending_reason'))
        elif status in ('confirmed', 'complete'):
            if self._btcpay_verify_amount(notification_data):
                self._set_done()
        elif status == 'new':
            pass  # Invoice created on BTCPay, waiting for the buyer to pay.
        elif status in ('expired', 'cancel', 'cancelled'):
            self._set_canceled(
                state_message="BTCPay: " + _("Invoice status: %s.", status))
        elif status == 'invalid':
            _logger.warning(
                "received invalid payment status for transaction with reference %s", self.reference)
            self._set_error(
                "BTCPay: " + _("Received data with invalid payment status: %s.", status))
        else:
            _logger.warning(
                "received unrecognized payment status (%s) for transaction with reference %s",
                status, self.reference)
            self._set_error(
                "BTCPay: " + _("Received data with unknown payment status: %s.", status))

    def _btcpay_verify_amount(self, notification_data):
        """ Check that the settled BTCPay invoice matches the transaction.

        The BTCPay invoice is created from the transaction, but its settled
        amount and currency are read back from BTCPay on notification and
        compared here so that a tampered or mismatched invoice can never
        confirm the transaction (and thus the order). On mismatch the
        transaction is set in error and ``False`` is returned.

        :param dict notification_data: The notification data sent by the provider
        :return: Whether the settled amount and currency match the transaction
        :rtype: bool
        """
        self.ensure_one()
        currency = notification_data.get('currency')
        try:
            amount = float(notification_data.get('amount'))
        except (TypeError, ValueError):
            amount = None
        if (amount is None
                or currency != self.currency_id.name
                or self.currency_id.compare_amounts(amount, self.amount) != 0):
            self._set_error("BTCPay: " + _(
                "The settled amount (%(amount)s %(currency)s) does not match the "
                "expected amount (%(expected)s %(expected_currency)s).",
                amount=notification_data.get('amount'), currency=currency,
                expected=self.amount, expected_currency=self.currency_id.name))
            return False
        return True
