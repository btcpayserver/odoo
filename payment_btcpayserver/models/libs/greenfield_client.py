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
                model = (detail.get('model') or '').strip()
                message = (detail.get('message') or '').strip()
                detail = ' '.join(part for part in (model, message) if part) or str(detail)
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

    # === WEBHOOKS === #

    def get_webhook(self, webhook_id):
        return self._request('GET', self._store_path(f'/webhooks/{webhook_id}'))

    def create_webhook(self, url, events=None, automatic_redelivery=True):
        payload = {
            'url': url,
            'automaticRedelivery': automatic_redelivery,
        }
        if events:
            payload['specificEvents'] = list(events)
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
