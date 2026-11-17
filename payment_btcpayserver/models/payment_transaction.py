import pprint

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.payment.logging import get_payment_logger

from .libs.greenfield_client import GreenfieldError

_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    btcpay_invoiceId = fields.Char("Invoice Id")
    btcpay_txid = fields.Char("Transaction Id")
    btcpay_status = fields.Char("Transaction Status")
    btcpay_refund_link = fields.Char(
        string="Refund Claim Link", copy=False,
        help="BTCPay refunds are pull payments: the customer must open this "
             "link and enter a destination (on-chain address or Lightning) to "
             "claim the refund. Send the link to the customer after creating "
             "the refund.")

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
            # Keep the pre-19.0.1.1 key during rolling deployments. QWeb
            # templates live in the database, so an instance restarted before
            # the module is upgraded can still render the previous template.
            'item_number': self.reference,
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

        Greenfield invoices have a two-axis state: ``status`` (New, Processing,
        Settled, Expired, Invalid) and ``additionalStatus`` (None, PaidPartial,
        PaidLate, PaidOver, Marked, Invalid). Both are always evaluated
        together; edge cases (partial, late and over payments) must never
        auto-confirm and always leave an explicit message for the operator.

        Note: self.ensure_one() from `_process`

        :param dict payment_data: The payment data sent by the provider.
        :return: None
        """
        if self.provider_code != 'btcpayserver':
            return super()._apply_updates(payment_data)

        _logger.info("BTCPay _apply_updates: %s", pprint.pformat(payment_data))

        if not payment_data.get('reference'):
            raise ValidationError(
                "BTCPay: " + _("Received payment data with missing reference."))

        # Keep both external-reference fields bound to the authenticated
        # BTCPay invoice. The controller validates this identity before
        # entering `_process`.
        if invoice_id := payment_data.get('invoiceID'):
            self.btcpay_invoiceId = invoice_id
            self.provider_reference = invoice_id
        if payment_data.get('txid'):
            self.btcpay_txid = payment_data['txid']
        status = (payment_data.get('status') or '').strip()
        additional = (payment_data.get('additionalStatus') or '').strip()
        if additional in ('', 'None'):
            additional = ''
        self.btcpay_status = f'{status}/{additional}' if additional else status

        if status == 'Processing':
            self._set_pending(state_message="BTCPay: " + _(
                "Payment seen in mempool, waiting for confirmations."))
        elif status == 'Settled':
            if additional == 'PaidOver':
                self._set_done(state_message="BTCPay: " + _(
                    "Invoice settled, but the customer overpaid - manual review required."))
            elif additional == 'Marked':
                self._set_done(state_message="BTCPay: " + _(
                    "Invoice settled (manually marked as settled in BTCPay)."))
            else:
                self._set_done()
        elif status == 'Expired':
            if additional in ('PaidPartial', 'PaidLate'):
                # A payment arrived although the invoice expired: it may be
                # incomplete (partial) or late. Never auto-confirm; the
                # operator decides (e.g. refund or manual confirmation).
                self._set_canceled(state_message="BTCPay: " + _(
                    "The invoice expired but a payment was received after expiry "
                    "(%s) - manual review required.", additional))
            else:
                self._set_canceled(
                    state_message="BTCPay: " + _("Invoice status: Expired."))
        elif status == 'Invalid':
            self._set_error(
                "BTCPay: " + _("Received data with invalid payment status: Invalid."))
        elif status == 'New':
            pass  # Invoice created on BTCPay, waiting for the buyer to pay.
        else:
            _logger.warning(
                "Received data with unknown payment status (%s/%s) for transaction with reference %s",
                status, additional, self.reference
            )
            self._set_error(
                "BTCPay: " + _("Received data with unknown payment status: %s.",
                               f'{status}/{additional}' if additional else status)
            )
        if self.state == 'done':
            self._btcpay_sync_order_payment_method()

    def _send_refund_request(self):
        """ Override of payment to create the refund on BTCPay.

        BTCPay refunds are pull payments, not direct transfers: the API call
        creates a pull payment for the invoice and returns a claim link that
        the customer must open to receive the money (on-chain address or
        Lightning). The refund transaction is therefore set to `pending`
        until the payout webhooks report the completion.

        The pull payment id is stored in `provider_reference` so the payout
        webhooks (PayoutCreated/PayoutApproved/PayoutUpdated) can match this
        refund transaction.

        Note: self.ensure_one() from `_refund`

        :return: None
        """
        if self.provider_code != 'btcpayserver':
            return super()._send_refund_request()

        self.ensure_one()
        source_tx = self.source_transaction_id
        if not source_tx or not source_tx.btcpay_invoiceId:
            raise ValidationError(_(
                "BTCPay: The original transaction has no BTCPay invoice to refund."))

        # A refund transaction has a negative amount; refunds of the full
        # paid amount use the configured variant, partial refunds a custom
        # amount in the transaction currency.
        full_refund = self.currency_id.compare_amounts(
            -self.amount, source_tx.amount) == 0
        if full_refund:
            payload = {'refundVariant': self.provider_id.btcpay_refund_variant}
        else:
            payload = {
                'refundVariant': 'Custom',
                'customAmount': str(-self.amount),
                'customCurrency': self.currency_id.name,
            }

        client = self.provider_id._btcpay_client()
        # BTCPay requires payout methods that are available for the original
        # invoice; without them it falls back to the store's default payout
        # method, which is often unset (422). Pass the store's enabled
        # methods that have a payout handler (ids ending in -CHAIN/-LN; e.g.
        # BTC-LNURL is a payment method without payout support and would be
        # rejected), so BTCPay can pick the closest match to what the
        # invoice was actually paid with (GetClosestPaymentMethodId).
        try:
            store_methods = client.get_store_payment_methods()
        except GreenfieldError as error:
            store_methods = None
            _logger.warning(
                "BTCPay: could not list store payment methods for the refund: %s",
                error,
            )
        if isinstance(store_methods, list):
            payout_method_ids = [
                method.get('paymentMethodId')
                for method in store_methods
                if isinstance(method, dict) and method.get('enabled')
                and method.get('paymentMethodId')
                and method['paymentMethodId'].endswith(('-CHAIN', '-LN'))
            ]
            if payout_method_ids:
                payload['payoutMethods'] = payout_method_ids
        try:
            refund = client.refund_invoice(source_tx.btcpay_invoiceId, payload)
        except GreenfieldError as error:
            raise ValidationError(_(
                "BTCPay: The refund request failed: %s", error)) from error

        pull_payment_id = refund.get('id')
        claim_link = refund.get('viewLink')
        if not pull_payment_id or not claim_link:
            raise ValidationError(_(
                "BTCPay: The refund response is missing the pull payment id or "
                "claim link."))

        # Store the pull payment id for the payout webhooks before anything
        # else; this write is a preparation that tolerates a rollback (a
        # redelivery of the refund action simply fails against BTCPay's
        # idempotency-free endpoint, so the operator retries instead).
        # Payment transactions have no chatter in Odoo 20: the claim link is
        # shown on the refund transaction (btcpay_refund_link field) and in
        # the state message.
        self.with_context(payment_safe_write=True).write({
            'provider_reference': pull_payment_id,
            'btcpay_refund_link': claim_link,
        })
        self.with_context(payment_safe_write=True)._set_pending(state_message="BTCPay: " + _(
            "Refund pull payment created, waiting for the customer to claim "
            "it via the refund link (see the Refund Claim Link field)."))
        _logger.info(
            "BTCPay: refund pull payment %s created for transaction %s "
            "(source invoice %s)", pull_payment_id, self.reference,
            source_tx.btcpay_invoiceId,
        )

    def _btcpay_sync_order_payment_method(self):
        """Default the sale orders' "Payment Method" to the provider's journal
        method line after a successful payment.

        Odoo leaves ``sale.order.preferred_payment_method_line_id`` empty
        unless the partner has a default inbound payment method line, so the
        later invoice payment registration has no method preselected. When the
        payment actually settled through BTCPay, defaulting it to the
        provider's method line (never overwriting a manual choice) books the
        payment on the provider's outstanding account automatically.
        """
        self.ensure_one()
        if 'sale_order_ids' not in self._fields:
            return  # sale module not installed
        method_line = self.env['account.payment.method.line'].search(
            [('payment_provider_id', '=', self.provider_id.id)], limit=1)
        if not method_line:
            return
        orders = self.sale_order_ids.filtered(
            lambda o: not o.preferred_payment_method_line_id)
        if orders:
            orders.preferred_payment_method_line_id = method_line
            _logger.info(
                "BTCPay: defaulted payment method to %s on orders %s",
                method_line.display_name, ', '.join(orders.mapped('name')),
            )
