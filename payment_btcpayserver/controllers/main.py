# ******************************************************************************
# PAYMENT BTCPAY FOR ODOO
#
# Copyright (C) 2020 Susanna Fort <susannafm@gmail.com>
#
# ******************************************************************************
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

import html as html_lib
import json

from werkzeug import urls

from odoo import _, http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.payment.logging import get_payment_logger
from ..models.libs.greenfield_client import GreenfieldClient, GreenfieldError


_logger = get_payment_logger(__name__)

_WEBHOOK_PATH = '/payment/btcpay/webhook'


def _btcpay_wizard_result_page(success, message, provider):
    """Standalone result page of the api-keys/authorize wizard.

    Served without a session on purpose: the user sees immediately whether
    the connection worked, before (re-)logging into the backend. The
    continue button deep-links to the provider form, which after the login
    opens exactly the record that was configured.
    """
    if provider is not None and provider.exists():
        action = request.env.ref('payment.action_payment_provider', raise_if_not_found=False)
        continue_url = urls.url_join(
            request.httprequest.url_root,
            f'/odoo/action-{action.id}/{provider.id}' if action else '/odoo')
        store_line = provider.btcpay_store_id or ''
    else:
        continue_url = urls.url_join(request.httprequest.url_root, '/odoo')
        store_line = ''
    icon, color = ('✅', '#017e84') if success else ('❌', '#c4392e')
    title = _('BTCPay Server connected!') if success else _('BTCPay setup was not completed')
    message = html_lib.escape(str(message))
    store_id = html_lib.escape(store_line)
    store_html = f'<p>Store: <strong>{store_id}</strong></p>' if store_id else ''
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               background: #f4f4f6; margin: 0; min-height: 100vh;
               display: flex; align-items: center; justify-content: center; }}
        .card {{ background: #fff; border-radius: 10px; box-shadow: 0 2px 12px rgba(0,0,0,.08);
                max-width: 460px; width: 90%; padding: 40px 36px; text-align: center; }}
        .icon {{ font-size: 52px; line-height: 1; }}
        h1 {{ font-size: 22px; margin: 18px 0 10px; color: #111; }}
        p {{ color: #555; font-size: 14px; line-height: 1.5; margin: 6px 0; }}
        a.btn {{ display: inline-block; margin-top: 22px; background: #714b67; color: #fff;
                text-decoration: none; padding: 10px 22px; border-radius: 6px; font-size: 14px; }}
        a.btn:hover {{ background: #5c3d55; }}
        small {{ color: #999; display: block; margin-top: 16px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon" style="color:{color}">{icon}</div>
        <h1>{title}</h1>
        <p>{message}</p>
        {store_html}
        <a class="btn" href="{continue_url}">Continue to Odoo</a>
        <small>You may need to log in again — the session cookie is not sent
               across the BTCPay redirect.</small>
    </div>
</body>
</html>"""
    return request.make_response(html, headers={'Content-Type': 'text/html; charset=utf-8'})


class BTCPayController(http.Controller):
    _checkout_url = '/btcpay/checkout'
    _return_url = '/payment/btcpay/return'
    _authorize_callback_path = '/payment/btcpay/authorize'

    @staticmethod
    def _find_transaction(reference):
        return request.env['payment.transaction'].sudo().search([
            ('reference', '=', reference),
            ('provider_code', '=', 'btcpayserver'),
        ], limit=1)

    @staticmethod
    def _lock_transaction(tx_sudo):
        """Lock a transaction until the current database transaction ends.

        Checkout and IPN requests can arrive concurrently. Serializing them on
        the payment transaction prevents two checkout requests from creating
        separate invoices and makes an IPN re-check the invoice ID after a
        checkout replaces an expired invoice.
        """
        tx_sudo.ensure_one()
        tx_sudo.flush_recordset()
        tx_sudo.env.cr.execute(
            "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE",
            [tx_sudo.id],
        )
        tx_sudo.invalidate_recordset([
            'state', 'btcpay_invoiceId', 'provider_reference',
            'state_message', 'btcpay_refund_link',
        ])

    @staticmethod
    def _ensure_invoice_id_is_unique(tx_sudo, invoice_id):
        """Reject an invoice ID already linked to another transaction."""
        other_tx = tx_sudo.env['payment.transaction'].sudo().search([
            ('id', '!=', tx_sudo.id),
            ('provider_code', '=', 'btcpayserver'),
            '|',
            ('btcpay_invoiceId', '=', invoice_id),
            ('provider_reference', '=', invoice_id),
        ], limit=1)
        if other_tx:
            raise ValidationError(_(
                "BTCPay: The invoice is already linked to another transaction."
            ))

    @classmethod
    def _validate_invoice_binding(
            cls, tx_sudo, incoming_invoice_id, incoming_order_id, fetched_invoice):
        """Bind the untrusted webhook payload to one authenticated invoice and transaction."""
        if not isinstance(fetched_invoice, dict):
            raise ValidationError(_("BTCPay: Received an invalid invoice response."))

        fetched_invoice_id = fetched_invoice.get('id')
        fetched_order_id = cls._get_invoice_order_id(fetched_invoice)
        if not (
            incoming_invoice_id
            and incoming_invoice_id == fetched_invoice_id
            and incoming_invoice_id == tx_sudo.btcpay_invoiceId
            and incoming_invoice_id == tx_sudo.provider_reference
        ):
            raise ValidationError(_(
                "BTCPay: The notification invoice does not match the transaction."
            ))
        # Greenfield stores the order reference in metadata.orderId (legacy
        # invoices fetched through Greenfield keep a top-level orderId). The
        # fetched invoice must belong to the transaction's reference; if the
        # payload itself carried an order id it must match as well.
        if not fetched_order_id or fetched_order_id != tx_sudo.reference:
            raise ValidationError(_(
                "BTCPay: The notification order does not match the transaction."
            ))
        if incoming_order_id and incoming_order_id != tx_sudo.reference:
            raise ValidationError(_(
                "BTCPay: The notification order does not match the transaction."
            ))
        cls._ensure_invoice_id_is_unique(tx_sudo, incoming_invoice_id)

    @classmethod
    def _find_transaction_by_invoice_id(cls, invoice_id):
        """Find exactly one BTCPay transaction from its stored invoice ID."""
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValidationError(_("BTCPay: Missing invoice ID in notification data."))

        txs_sudo = request.env['payment.transaction'].sudo().search([
            ('provider_code', '=', 'btcpayserver'),
            ('btcpay_invoiceId', '=', invoice_id),
        ], limit=2)
        if len(txs_sudo) != 1:
            raise ValidationError(_(
                "BTCPay: No unique transaction found matching the invoice ID."
            ))
        return txs_sudo

    @staticmethod
    def _get_invoice_order_id(invoice):
        """Return the order reference stored on the invoice.

        Greenfield keeps it in ``metadata.orderId``; invoices created through
        the legacy API expose a top-level ``orderId`` when fetched via
        Greenfield.
        """
        if not isinstance(invoice, dict):
            return None
        metadata = invoice.get('metadata') or {}
        return metadata.get('orderId') or invoice.get('orderId')

    @staticmethod
    def _validate_invoice_amount(tx_sudo, invoice):
        """Reject reuse of an invoice with a different amount or currency."""
        try:
            amount = float(invoice.get('amount'))
        except (AttributeError, TypeError, ValueError):
            amount = None
        if (
            amount is None
            or invoice.get('currency') != tx_sudo.currency_id.name
            or tx_sudo.currency_id.compare_amounts(amount, tx_sudo.amount) != 0
        ):
            raise ValidationError(_(
                "BTCPay: The existing invoice amount or currency does not match the transaction."
            ))

    @classmethod
    def _validate_existing_invoice(cls, tx_sudo, invoice_id, invoice):
        """Validate a stored invoice before redirecting the customer to it."""
        cls._validate_invoice_binding(
            tx_sudo, invoice_id, tx_sudo.reference, invoice,
        )
        if not isinstance(invoice.get('status'), str) or not invoice['status']:
            raise ValidationError(_("BTCPay: The existing invoice response is incomplete."))

    @classmethod
    def _validate_created_invoice(cls, tx_sudo, invoice):
        """Validate a newly created invoice before storing its identity."""
        if not isinstance(invoice, dict):
            raise ValidationError(_("BTCPay: Received an invalid invoice response."))
        invoice_id = invoice.get('id')
        if (
            not isinstance(invoice_id, str)
            or not invoice_id
            or not isinstance(invoice.get('checkoutLink'), str)
            or not invoice['checkoutLink']
            or cls._get_invoice_order_id(invoice) not in (None, tx_sudo.reference)
        ):
            raise ValidationError(_("BTCPay: The created invoice response is incomplete."))
        cls._ensure_invoice_id_is_unique(tx_sudo, invoice_id)

    @http.route(_checkout_url, type='http', auth='public', methods=['POST'],
                csrf=False, website=True)
    def checkout(self, **data):
        """ Create the BTCPay invoice and redirect the buyer to it.

        The redirect form only carries the transaction reference. The invoice
        price, currency and buyer details are read from the transaction
        server-side and are never taken from the (client-controlled) request
        data, so a buyer cannot have an invoice created for a tampered amount.
        """
        _logger.info("BTCPay: checkout request for transaction %s", data.get('reference'))
        reference = data.get('reference')
        tx_sudo = self._find_transaction(reference)
        if not tx_sudo:
            raise ValidationError(
                _("BTCPay: No transaction found matching reference %s.", reference))
        self._lock_transaction(tx_sudo)
        if tx_sudo.state != 'draft':
            # Already processed (e.g. double submission): fall back to the
            # generic payment status page.
            return request.redirect('/payment/status')

        provider = tx_sudo.provider_id
        base_url = provider.get_base_url()
        client = provider._btcpay_client()

        # A browser retry must not create a second payable invoice. Reuse the
        # stored invoice while it remains payable. If it expired or became
        # invalid (e.g. double-spend), the new ID is stored before the lock
        # is released, so late notifications for the old invoice can no
        # longer select this transaction.
        if tx_sudo.btcpay_invoiceId:
            try:
                existing_invoice = client.get_invoice(tx_sudo.btcpay_invoiceId)
            except GreenfieldError as error:
                # BTCPay unreachable/erroring: a raw 500 here would hit the
                # buyer. Convert to a controlled, customer-safe error. The
                # detail (host, endpoint, API message) is logged for the
                # operator but never shown to the buyer.
                _logger.warning(
                    "BTCPay: fetching stored invoice %s for transaction %s "
                    "failed: %s",
                    tx_sudo.btcpay_invoiceId, tx_sudo.reference, error)
                raise ValidationError(_(
                    "BTCPay: The stored invoice could not be retrieved. "
                    "Please try again shortly."))
            self._validate_existing_invoice(
                tx_sudo, tx_sudo.btcpay_invoiceId, existing_invoice,
            )
            # 'invalid' invoices are dead (double-spend / network conflict):
            # like expired ones they must be replaced, not reused, or the
            # buyer would be sent to a dead checkout page.
            if existing_invoice['status'].lower() not in ('expired', 'invalid'):
                self._validate_invoice_amount(tx_sudo, existing_invoice)
                if (
                    not isinstance(existing_invoice.get('checkoutLink'), str)
                    or not existing_invoice['checkoutLink']
                ):
                    raise ValidationError(_(
                        "BTCPay: The existing invoice response is incomplete."
                    ))
                _logger.info(
                    "BTCPay: reusing invoice %s for transaction %s",
                    tx_sudo.btcpay_invoiceId, tx_sudo.reference,
                )
                return request.redirect(existing_invoice['checkoutLink'], local=False)

        try:
            invoice = client.create_invoice(
                amount=tx_sudo.amount,
                currency=tx_sudo.currency_id.name,
                metadata={'orderId': tx_sudo.reference},
                checkout={
                    'redirectURL': urls.url_join(base_url, self._return_url),
                    'redirectAutomatically': True,
                },
            )
        except GreenfieldError as error:
            # Same as above: keep BTCPay outages away from the buyer as a
            # controlled error instead of a raw 500 page. Log the detail,
            # show the buyer a generic message only.
            _logger.warning(
                "BTCPay: creating invoice for transaction %s failed: %s",
                tx_sudo.reference, error)
            raise ValidationError(_(
                "BTCPay: The payment invoice could not be created. Please "
                "try again shortly."))
        self._validate_created_invoice(tx_sudo, invoice)
        # Since Odoo 20.0, payment.transaction.write() requires the
        # payment_safe_write context flag. Storing the freshly created invoice
        # id is a preparation write: if the request rolls back, the next
        # checkout simply creates a new invoice (the unbound one expires).
        tx_sudo.with_context(payment_safe_write=True).write({
            'btcpay_invoiceId': invoice['id'],
            'provider_reference': invoice['id'],
        })
        _logger.info("BTCPay: created invoice %s for transaction %s",
                     invoice['id'], tx_sudo.reference)
        return request.redirect(invoice['checkoutLink'], local=False)

    @http.route('/payment/btcpay/webhook', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def btcpay_webhook(self, **post):
        """ BTCPay Greenfield webhook (store-level, HMAC-signed). """
        _logger.info('BTCPay: webhook received')
        raw_body = request.httprequest.get_data()
        signature = request.httprequest.headers.get('BTCPay-Sig')
        try:
            data = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _logger.warning("BTCPay: received malformed JSON webhook data")
            return request.make_response('malformed JSON', status=400)
        if not isinstance(data, dict):
            _logger.warning("BTCPay: received non-object webhook data")
            return request.make_response('invalid payload', status=400)

        # The store id selects the provider (and thus the secret); signature
        # verification then authenticates the payload itself.
        provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'btcpayserver'),
            ('btcpay_store_id', '=', data.get('storeId') or ''),
        ], limit=1)
        if not provider or not provider.btcpay_webhook_secret:
            _logger.warning(
                "BTCPay: webhook for unknown or unconnected store %s",
                data.get('storeId'),
            )
            return request.make_response('unknown store', status=400)
        if not GreenfieldClient.is_valid_webhook_signature(
                raw_body, signature, provider.btcpay_webhook_secret):
            _logger.warning("BTCPay: invalid webhook signature, rejecting")
            return request.make_response('invalid signature', status=400)

        event_type = data.get('type') or ''
        if event_type.startswith('Payout'):
            # Refund lifecycle events: no invoiceId, matched via the pull
            # payment id stored on the refund transaction.
            try:
                self._btcpay_process_payout_webhook(provider, data)
            except ValidationError as error:
                _logger.warning("BTCPay: rejected payout webhook: %s", error)
            return ''
        incoming_invoice_id = data.get('invoiceId')
        if not incoming_invoice_id:
            # Events without an invoice (e.g. webhook test notifications).
            _logger.info("BTCPay: ignored webhook event %s (no invoice)", event_type)
            return ''
        if data.get('isRedelivery'):
            _logger.info("BTCPay: webhook %s is a redelivery", event_type)

        try:
            # Never trust payload data to select the transaction: the stored
            # invoice ID is the lookup key.
            tx_sudo = self._find_transaction_by_invoice_id(incoming_invoice_id)

            # Fetching through the authenticated API establishes the invoice's
            # current state. Lock afterwards and re-check the stored ID so a
            # concurrent checkout cannot replace it between lookup and use.
            fetched_invoice = provider._btcpay_client().get_invoice(incoming_invoice_id)
            self._lock_transaction(tx_sudo)
            self._validate_invoice_binding(
                tx_sudo, incoming_invoice_id, data.get('orderId'), fetched_invoice,
            )
            _logger.info(
                "BTCPay: verified invoice %s with status %s/%s for transaction %s (event %s)",
                fetched_invoice.get('id'), fetched_invoice.get('status'),
                fetched_invoice.get('additionalStatus'), tx_sudo.reference, event_type,
            )

            payment_data = {
                "reference": tx_sudo.reference,
                "status": fetched_invoice.get('status'),
                "additionalStatus": fetched_invoice.get('additionalStatus'),
                "invoiceID": incoming_invoice_id,
                "txid": fetched_invoice.get('checkoutLink'),
                # Settled amount and currency, validated against the transaction
                # by the payment framework before it is set done.
                "amount": fetched_invoice.get('amount'),
                "currency": fetched_invoice.get('currency'),
            }

            # Since Odoo 20.0, _process() takes no provider code anymore and
            # payment data must go through _record(): it is stored as a
            # payment.data record and applied by the processing cron with
            # proper locking. The /payment/status page triggers immediate
            # processing when the customer is redirected back to the shop.
            tx_sudo._record(payment_data)
        except ValidationError as error:
            _logger.warning("BTCPay: rejected webhook: %s", error)
        return ''

    @classmethod
    def _btcpay_process_payout_webhook(cls, provider, data):
        """Process a payout webhook for a refund pull payment.

        BTCPay refunds are pull payments; the customer claims them and the
        store then pays out. The refund transaction was created by
        `_send_refund_request` with the pull payment id in
        `provider_reference`, which is the only trusted lookup key here (the
        payload is authenticated by the webhook signature, but the matching
        must be done against stored data, not payload data).

        Event shapes (top-level fields, verified against BTCPay Server's
        PayoutTriggerProvider): ``type`` PayoutCreated/PayoutApproved/
        PayoutUpdated, plus ``payoutId``, ``payoutState``
        (AwaitingApproval/AwaitingPayment/InProgress/Completed/Cancelled) and
        ``pullPaymentId``.

        :param provider: The BTCPay provider the webhook was registered on.
        :param dict data: The verified webhook payload.
        :return: None
        """
        event_type = data.get('type') or ''
        pull_payment_id = data.get('pullPaymentId')
        payout_state = data.get('payoutState') or ''
        if not pull_payment_id:
            _logger.info(
                "BTCPay: ignored webhook event %s (no pull payment id)",
                event_type,
            )
            return
        refund_txs = request.env['payment.transaction'].sudo().search([
            ('provider_id', '=', provider.id),
            ('operation', '=', 'refund'),
            ('provider_reference', '=', pull_payment_id),
        ])
        if not refund_txs:
            _logger.info(
                "BTCPay: no refund transaction found for pull payment %s"
                " (event %s)", pull_payment_id, event_type,
            )
            return

        for refund_tx in refund_txs:
            cls._lock_transaction(refund_tx)
            _logger.info(
                "BTCPay: payout webhook %s (%s) for refund transaction %s",
                event_type, payout_state, refund_tx.reference,
            )
            if payout_state == 'Completed':
                # Same write pattern as the payment data cron: locked record,
                # payment_safe_write context.
                refund_tx.with_context(payment_safe_write=True)._set_done(
                    state_message="BTCPay: " + _(
                        "Refund payout completed - the money was sent to the "
                        "customer's claimed destination."))
            else:
                # Transactions have no chatter in Odoo 20: informational
                # updates go into state_message, shown on the refund form.
                # Only pending transactions track payout progress; a late
                # event must not overwrite the final state's message.
                if refund_tx.state == 'pending':
                    message = {
                        'Cancelled': _(
                            "BTCPay: the refund payout was cancelled in "
                            "BTCPay - handle it manually (e.g. recreate the "
                            "payout on the store)."),
                    }.get(payout_state) or _(
                        "BTCPay: refund payout state update: %s (%s).",
                        payout_state, event_type,
                    )
                    refund_tx.with_context(payment_safe_write=True).write({
                        'state_message': message,
                    })

    @http.route(_return_url, type='http', auth="public", methods=['GET'],
                csrf=False, save_session=False)
    def btcpay_return_from_redirect(self, **data):
        """ BTCPay return """
        _logger.info("BTCPay: user returned to shop after payment")
        return request.redirect('/payment/status')

    @http.route('/payment/btcpay/authorize', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False, save_session=True)
    def btcpay_authorize_callback(self, **post):
        """ Callback of the BTCPay api-keys/authorize wizard (Phase 2).

        BTCPay submits an auto-submitted POST form to the exact redirect URL
        the wizard started from, containing ``apiKey`` and ``permissions[]``
        (store-scoped, e.g. ``btcpay.store.canviewinvoices:<storeId>``).
        Provider identity and the signed state travel in the redirect URL's
        query string (BTCPay does not echo query params back in the form).

        This route must NOT require a user session: the POST originates from
        the BTCPay server's page (cross-site), so the browser does not send
        the Odoo session cookie (SameSite=Lax default). Authenticity comes
        exclusively from the HMAC state token issued when the wizard
        started; the credentials are applied atomically and on any failure
        the previous configuration is untouched.
        """
        httprequest = request.httprequest
        # Credentials travel only in the POST body of the form BTCPay
        # submits; never accept them from the query string - a key in the
        # URL would end up in web-server and proxy access logs. NOTE: Odoo
        # merges the query string into the controller's **post kwargs
        # (http.get_http_params()), so post.get('apiKey') would still read
        # the URL - only httprequest.form is guaranteed body-only.
        api_key = httprequest.form.get('apiKey')
        permissions = (httprequest.form.getlist('permissions[]')
                       or httprequest.form.getlist('permissions'))
        provider_id = httprequest.args.get('provider', type=int)
        state = httprequest.args.get('state')

        if not api_key or not provider_id or not state:
            _logger.warning(
                'BTCPay: wizard callback incomplete (method=%s, apiKey=%s, provider=%s)',
                httprequest.method, bool(api_key), provider_id)
            return _btcpay_wizard_result_page(False, _(
                'The BTCPay authorization callback is incomplete. Please start '
                'the setup wizard again from the payment provider settings.'), None)
        provider = request.env['payment.provider'].sudo().browse(provider_id)
        if not provider.exists() or provider.code != 'btcpayserver':
            _logger.warning('BTCPay: wizard callback for unknown provider %s', provider_id)
            return _btcpay_wizard_result_page(False, _(
                'The authorization refers to an unknown payment provider. Please '
                'start the setup wizard again from the payment provider settings.'), None)
        # The state was signed over "<id>|<ts>|<callback base>|<write_date>";
        # the callback base must be identical on both sides, so it is derived
        # from web.base.url via get_base_url() — NOT from the incoming
        # request: behind a reverse proxy or with a differing host the
        # request-derived base would reject legitimate callbacks (fail-closed,
        # but breaks the wizard). The write_date binding makes the state
        # single-use: applying credentials invalidates the token at once.
        redirect_uri = urls.url_join(
            provider.get_base_url(), self._authorize_callback_path)
        if not provider._btcpay_wizard_state_verify(redirect_uri, state):
            _logger.warning(
                'BTCPay: wizard callback state verification failed (provider %s)',
                provider_id)
            return _btcpay_wizard_result_page(False, _(
                'The authorization could not be verified (invalid or expired '
                'signature). Please start the setup wizard again from the '
                'payment provider settings.'), provider)
        ok, message = provider._btcpay_apply_wizard_credentials(api_key, permissions)
        _logger.info('BTCPay: wizard credentials applied for provider %s: ok=%s, %s',
                     provider.id, ok, message)
        return _btcpay_wizard_result_page(ok, message, provider)
