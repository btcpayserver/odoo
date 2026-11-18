"""BTCPay Server Greenfield API client.

Thin wrapper over ``requests`` covering the endpoints needed by the
payment provider: store info (connection test), invoices and webhook
lifecycle. Authentication uses an API key scoped to a single store:
``Authorization: token <api_key>``.

Webhook signature verification follows the BTCPay Server convention
(also implemented in btcpayserver-greenfield-php
``Webhook::isIncomingWebhookRequestValid``):
``BTCPay-Sig: sha256=<hex(hmac_sha256(raw_body, webhook_secret))>``.
"""

import hashlib
import hmac

import requests

WEBHOOK_EVENTS = [
    'InvoiceReceivedPayment',
    'InvoicePaymentSettled',
    'InvoiceProcessing',
    'InvoiceExpired',
    'InvoiceSettled',
    'InvoiceInvalid',
    # Refund lifecycle: a refund creates a pull payment on the store; its
    # payouts are claimed by the customer and then paid out. States travel
    # AwaitingApproval -> AwaitingPayment -> InProgress -> Completed (or
    # Cancelled); every change fires PayoutUpdated with the new state.
    'PayoutCreated',
    'PayoutApproved',
    'PayoutUpdated',
]


class GreenfieldError(Exception):
    """Raised when a Greenfield API call fails or is unreachable."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GreenfieldClient:
    def __init__(self, host, api_key, store_id, timeout=30):
        self.host = (host or '').rstrip('/')
        self.api_key = api_key or ''
        self.store_id = store_id or ''
        self.timeout = timeout

    # === INTERNALS === #

    def _request(self, method, path, payload=None):
        url = f'{self.host}/api/v1{path}'
        headers = {
            'Authorization': f'token {self.api_key}',
            'Accept': 'application/json',
        }
        try:
            response = requests.request(
                method, url, headers=headers, json=payload, timeout=self.timeout,
            )
        except requests.exceptions.RequestException as error:
            raise GreenfieldError(
                f'Could not reach BTCPay Server at {self.host}: {error}',
            ) from error
        if response.status_code >= 400:
            try:
                detail = response.json()
                if isinstance(detail, list):
                    # Validation errors arrive as a list of problem details.
                    detail = ' '.join(
                        ' '.join(str(v) for v in item.values())
                        if isinstance(item, dict) else str(item)
                        for item in detail
                    ).strip()
                elif isinstance(detail, dict):
                    model = (detail.get('model') or '').strip()
                    message = (detail.get('message') or '').strip()
                    detail = ' '.join(part for part in (model, message) if part) or str(detail)
                else:
                    detail = str(detail)
            except ValueError:
                detail = response.text[:200]
            raise GreenfieldError(
                f'BTCPay API {method} {path} failed '
                f'({response.status_code}): {detail}',
                status=response.status_code,
            )
        if not response.content:
            return None
        return response.json()

    def _store_path(self, suffix=''):
        return f'/stores/{self.store_id}{suffix}'

    # === STORE === #

    def get_store(self):
        """Fetch the configured store (connection test)."""
        return self._request('GET', self._store_path())

    def get_store_payment_methods(self):
        """List the store's payment/payout methods (e.g. BTC-CHAIN, BTC-LN)."""
        return self._request('GET', self._store_path('/payment-methods'))

    # === INVOICES === #

    def create_invoice(self, amount, currency, metadata=None, checkout=None):
        body = {
            'amount': str(amount),
            'currency': currency,
            'metadata': metadata or {},
        }
        if checkout:
            body['checkout'] = checkout
        return self._request('POST', self._store_path('/invoices'), body)

    def get_invoice(self, invoice_id):
        return self._request('GET', self._store_path(f'/invoices/{invoice_id}'))

    # === REFUNDS === #

    def refund_invoice(self, invoice_id, payload):
        """Create a refund pull payment for an invoice.

        ``payload`` is a ``RefundInvoiceRequest``: ``refundVariant`` (Fiat,
        RateThen, CurrentRate, OverpaidAmount or Custom) plus customAmount/
        customCurrency for the Custom variant. Requires the
        ``btcpay.store.cancreatenonapprovedpullpayments`` permission and a
        refundable invoice (Settled). The response is a ``PullPaymentData``
        whose ``viewLink`` is the claim link the customer must open to
        actually receive the refund.
        """
        return self._request(
            'POST', self._store_path(f'/invoices/{invoice_id}/refund'), payload,
        )

    # === WEBHOOKS === #

    def get_webhook(self, webhook_id):
        return self._request('GET', self._store_path(f'/webhooks/{webhook_id}'))

    def create_webhook(self, url, events=None, automatic_redelivery=True):
        payload = {
            'url': url,
            'automaticRedelivery': automatic_redelivery,
        }
        if events:
            # The API model expects authorizedEvents.specificEvents; a bare
            # top-level "specificEvents" key is silently ignored and the
            # webhook falls back to authorizedEvents.everything=true.
            payload['authorizedEvents'] = {
                'everything': False,
                'specificEvents': list(events),
            }
        return self._request('POST', self._store_path('/webhooks'), payload)

    def delete_webhook(self, webhook_id):
        return self._request('DELETE', self._store_path(f'/webhooks/{webhook_id}'))

    # === WEBHOOK SIGNATURE === #

    @staticmethod
    def is_valid_webhook_signature(raw_body, signature_header, secret):
        """Verify the ``BTCPay-Sig`` header of a webhook request."""
        if not (raw_body and signature_header and secret):
            return False
        expected = 'sha256=' + hmac.new(
            secret.encode('utf-8'), raw_body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    # === API KEY PERMISSIONS === #

    # Permissions required by this integration, in the order they should be
    # reported when missing. ``cancreateinvoice`` cannot be probed harmlessly
    # and is expected in the same key as ``canmodifyinvoices``.
    REQUIRED_PERMISSIONS = [
        'btcpay.store.canviewinvoices',
        'btcpay.store.cancreateinvoice',
        'btcpay.store.canmodifyinvoices',
        'btcpay.store.webhooks.canmodifywebhooks',
        'btcpay.store.canviewstoresettings',
        'btcpay.store.cancreatenonapprovedpullpayments',
    ]

    def get_current_api_key(self):
        """Return the ApiKeyData of the key used for authentication
        (label, permissions incl. store scopes)."""
        return self._request('GET', '/api-keys/current')

    def check_permissions(self, store_id, required=None):
        """Verify the API key's permissions and store scoping.

        Uses the current-api-key endpoint: it exposes the exact permission
        list of the key, where store-scoped permissions carry a
        ``:<storeId>`` suffix. Un-scoped (server-wide) keys are accepted but
        reported, since the guide recommends binding keys to one store.

        :param str store_id: the store the integration is configured for
        :param list required: permissions to check (defaults to all required)
        :return: (ok: bool, missing: list[str], warnings: list[str])
        """
        required = list(required or self.REQUIRED_PERMISSIONS)
        key_info = self.get_current_api_key()
        granted = key_info.get('permissions') or []

        warnings = []
        # a permission granted without a scope applies server-wide
        bare_permissions = {p for p in granted if ':' not in p}
        # a permission granted with ':<storeId>' applies to that store only
        scoped_permissions = {
            (p.split(':', 1)[0], p.split(':', 1)[1])
            for p in granted if ':' in p
        }

        missing = []
        for permission in required:
            if (permission, store_id) in scoped_permissions:
                continue  # properly scoped to the configured store
            if permission in bare_permissions:
                # granted server-wide: works, but the guide recommends a
                # store-bound key
                warnings.append(permission)
                continue
            missing.append(permission)

        if not granted:
            missing.extend(p for p in required if p not in missing)
        return (not missing), missing, warnings
