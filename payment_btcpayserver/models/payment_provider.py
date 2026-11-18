import hashlib
import hmac
import time

from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.payment.logging import get_payment_logger

from .libs.greenfield_client import GreenfieldClient, GreenfieldError, WEBHOOK_EVENTS

_logger = get_payment_logger(__name__, sensitive_keys=['apiKey', 'secret'])

_WEBHOOK_PATH = '/payment/btcpay/webhook'
_AUTHORIZE_CALLBACK_PATH = '/payment/btcpay/authorize'


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('btcpayserver', "BTCPay")], ondelete={'btcpayserver': 'set default'})

    btcpay_location = fields.Char(
        string='BTCPay Server URL', size=64,
        help='URL where your BTCPay Server instance is reachable (where you log into your BTCPay Server).',
        default='https://testnet.demo.btcpayserver.org')
    btcpay_api_key = fields.Char(
        string='API Key', copy=False,
        help='Greenfield API key scoped to a single store, with the permissions: '
             'btcpay.store.canviewinvoices, btcpay.store.cancreateinvoice, '
             'btcpay.store.canmodifyinvoices, btcpay.store.webhooks.canmodifywebhooks, '
             'btcpay.store.canviewstoresettings, btcpay.store.cancreatenonapprovedpullpayments')
    btcpay_store_id = fields.Char(
        string='Store ID', copy=False,
        help='ID of the BTCPay Server store invoices are created for.')
    btcpay_webhook_secret = fields.Char(
        string='Webhook Secret', copy=False,
        help='Secret returned when the webhook was registered; used to verify '
             'incoming webhook signatures.')
    btcpay_webhook_id = fields.Char(
        string='Webhook ID', copy=False,
        help="ID of the webhook registered on the BTCPay store. The literal value "
             "'manual' means the webhook was created in the BTCPay UI and its "
             "secret was pasted here by hand.")
    btcpay_webhook_url = fields.Char(
        string='Webhook Endpoint', compute='_compute_btcpay_webhook_url',
        help='URL that will be registered on the BTCPay store to receive '
             'invoice status webhooks. Must be reachable over public https.')
    btcpay_store_name = fields.Char(
        string='BTCPay Store Name', readonly=True, copy=False,
        help='Name of the configured store as returned by BTCPay (filled by the '
             'connection test).')
    btcpay_connection_status = fields.Char(
        string='Connection Status', readonly=True, copy=False)
    btcpay_webhook_status = fields.Char(
        string='Webhook Status', readonly=True, copy=False)
    btcpay_show_manual = fields.Boolean(
        string='Manual Setup', copy=False,
        help='Reveal the API key / store id / webhook secret fields to enter '
             'the credentials by hand instead of using the setup wizard.')
    btcpay_authorize_url = fields.Char(
        string='BTCPay Authorization URL', compute='_compute_btcpay_authorize_url',
        help='BTCPay page where the user authorizes Odoo to create invoices on '
             'one store and manage the webhook. Redirects back with the API key.')
    btcpay_refund_variant = fields.Selection(
        selection=[
            ('Fiat', 'Fiat - invoice currency, current rate at payout time'),
            ('RateThen', 'RateThen - crypto amount at the rate when the invoice was paid'),
            ('CurrentRate', 'CurrentRate - crypto amount at the current rate'),
        ],
        string='Refund Variant', default='Fiat', required=True,
        help='What a full refund pays out. Fiat refunds the invoice currency '
             '(e.g. EUR 100) at the rate when the refund is sent - the '
             'customer carries the rate risk. RateThen/CurrentRate refund the '
             'crypto amount the invoice was paid in. Partial refunds always '
             'use a custom amount in the transaction currency.')

    # === COMPUTED === #

    def _compute_feature_support_fields(self):
        """ Override of payment to add the BTCPay-specific feature support.

        The Greenfield API creates refunds as pull payments for any amount
        (custom variant), so both full and partial refunds are supported.
        """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'btcpayserver').support_refund = 'partial'

    def _compute_btcpay_webhook_url(self):
        for provider in self:
            try:
                base_url = provider.get_base_url()
            except Exception:
                base_url = ''
            provider.btcpay_webhook_url = urls.url_join(base_url, _WEBHOOK_PATH) if base_url else False

    def _compute_btcpay_authorize_url(self):
        for provider in self:
            provider.btcpay_authorize_url = provider._btcpay_authorize_url()

    # === HELPERS === #

    def _btcpay_client(self):
        self.ensure_one()
        return GreenfieldClient(
            host=self.btcpay_location,
            api_key=self.btcpay_api_key,
            store_id=self.btcpay_store_id,
        )

    def _btcpay_credentials_complete(self):
        self.ensure_one()
        return bool(self.btcpay_location and self.btcpay_api_key and self.btcpay_store_id)

    def _btcpay_authorize_url(self):
        """Build the BTCPay api-keys/authorize redirect URL for this provider.

        The ``redirect`` handed to BTCPay is our callback URL carrying the
        provider id and an HMAC ``state`` in its query string — BTCPay posts
        its auto-submit form to that exact URL, so both survive the round
        trip (the POST body itself only contains apiKey/userId/permissions).
        The state is signed over ``<id>|<callback base url>`` and re-verified
        on the callback, so credentials can only be applied to the provider
        that started the wizard.
        """
        self.ensure_one()
        if not self.btcpay_location:
            return False
        base_url = self.get_base_url()
        callback_base = urls.url_join(base_url, _AUTHORIZE_CALLBACK_PATH)
        state = self._btcpay_wizard_state_sign(callback_base)
        redirect_uri = f'{callback_base}?{urls.url_encode({"provider": self.id, "state": state})}'
        query = {
            'permissions': GreenfieldClient.REQUIRED_PERMISSIONS,
            'applicationName': 'Odoo',
            'strict': 'true',
            'selectiveStores': 'true',
            'redirect': redirect_uri,
        }
        return f"{self.btcpay_location.rstrip('/')}/api-keys/authorize?{urls.url_encode(query)}"

    def _btcpay_wizard_state_sign(self, callback_base):
        """Build the signed wizard state for this provider.

        Format: ``<unix-ts>.<hex hmac>`` where the HMAC covers
        ``<id>|<ts>|<callback base>|<write_date>``. The timestamp lets the
        callback reject stale states (the state travels in the redirect URL
        and could leak via referrers; a captured state must only be usable
        for a short window around the wizard run). The ``write_date`` makes
        the state **single-use**: applying credentials writes the provider,
        which moves ``write_date`` and invalidates the state immediately —
        a captured or replayed state is worthless after the first
        successful callback (or after any unrelated save of the provider).
        """
        self.ensure_one()
        secret = self.env['ir.config_parameter'].sudo().get_str('database.secret')
        ts = int(time.time())
        payload = f'{self.id}|{ts}|{callback_base}|{self.write_date or ""}'
        signature = hmac.new(
            secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256,
        ).hexdigest()
        return f'{ts}.{signature}'

    def _btcpay_wizard_state_verify(self, callback_base, state, max_age=900):
        """Verify a wizard state token (format ``<ts>.<hex hmac>``).

        Rejects stale states (older than ``max_age`` seconds) and states
        signed for a previous provider state: the HMAC covers the
        provider's ``write_date``, so once credentials are applied (or the
        provider saved at all) the token no longer verifies.
        """
        self.ensure_one()
        if not state or '.' not in state:
            return False
        ts_part, _, signature = state.partition('.')
        if not ts_part.isdigit():
            return False
        ts = int(ts_part)
        if abs(int(time.time()) - ts) > max_age:
            return False
        secret = self.env['ir.config_parameter'].sudo().get_str('database.secret')
        payload = f'{self.id}|{ts}|{callback_base}|{self.write_date or ""}'
        expected = hmac.new(
            secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _btcpay_apply_wizard_credentials(self, api_key, permissions):
        """Atomically swap in wizard credentials (MIGRATION-GREENFIELD.md §Phase 2).

        1. validate the returned key + resolve its scoped store id,
        2. register the webhook for the new store,
        3. only then store the new credentials,
        4. delete the old webhook with the old credentials (best effort).
        On failure nothing is written and the error message is returned.
        """
        self.ensure_one()
        old = {
            'btcpay_api_key': self.btcpay_api_key,
            'btcpay_store_id': self.btcpay_store_id,
            'btcpay_webhook_secret': self.btcpay_webhook_secret,
            'btcpay_webhook_id': self.btcpay_webhook_id,
        }
        # 1) validate the new key; the store id comes from a scoped permission
        scoped_stores = {
            p.split(':', 1)[1] for p in permissions
            if ':' in p and p.split(':', 1)[0] in (
                'btcpay.store.canviewinvoices', 'btcpay.store.canviewstoresettings')
        }
        if len(scoped_stores) != 1:
            return False, _(
                'The BTCPay authorization did not return exactly one store '
                '(got: %s). Re-run the wizard and pick a single store.',
                ', '.join(sorted(scoped_stores)) or _('none'))
        store_id = scoped_stores.pop()
        client = GreenfieldClient(
            host=self.btcpay_location, api_key=api_key, store_id=store_id)
        try:
            store = client.get_store()
            if store.get('id') != store_id:
                return False, _(
                    'The authorized API key resolves to store %s, not %s.',
                    store.get('id'), store_id)
            ok, missing, _warnings = client.check_permissions(store_id)
        except GreenfieldError as error:
            return False, str(error)
        if not ok:
            return False, _(
                'The authorized API key is missing permissions: %s',
                ', '.join(missing))
        # 2) register the webhook on the new store
        try:
            endpoint_url = urls.url_join(self.get_base_url(), _WEBHOOK_PATH)
            hook = client.create_webhook(endpoint_url, events=WEBHOOK_EVENTS)
            if not hook.get('secret'):
                client.delete_webhook(hook.get('id'))
                return False, _('BTCPay did not return a webhook secret.')
        except GreenfieldError as error:
            return False, str(error)
        # 3) store the new credentials
        self.with_context(btcpay_skip_webhook_sync=True).write({
            'btcpay_api_key': api_key,
            'btcpay_store_id': store_id,
            'btcpay_webhook_id': hook.get('id'),
            'btcpay_webhook_secret': hook.get('secret'),
            'btcpay_store_name': store.get('name'),
            'btcpay_connection_status': _(
                '✅ Connected via setup wizard: store "%s".', store.get('name')),
        })
        # 4) best-effort cleanup of the old webhook
        warning = None
        if old['btcpay_webhook_id'] and old['btcpay_webhook_id'] != 'manual' \
                and old['btcpay_api_key'] and old['btcpay_store_id']:
            old_client = GreenfieldClient(
                host=self.btcpay_location,
                api_key=old['btcpay_api_key'],
                store_id=old['btcpay_store_id'])
            try:
                old_client.delete_webhook(old['btcpay_webhook_id'])
            except GreenfieldError as error:
                warning = _(
                    'The previous webhook %s could not be deleted on store %s '
                    '(%s) - remove it manually in the store settings.',
                    old['btcpay_webhook_id'], old['btcpay_store_id'], error)
        self._btcpay_update_status(
            webhook=_('Registered (webhook id: %s).', hook.get('id')))
        return True, warning or _('Connected to store "%s".', store.get('name'))

    def _btcpay_update_status(self, connection=None, webhook=None, store_name=False):
        """Persist status texts without re-triggering the webhook sync."""
        vals = {}
        if connection is not None:
            vals['btcpay_connection_status'] = connection
        if webhook is not None:
            vals['btcpay_webhook_status'] = webhook
        if store_name is not False:
            vals['btcpay_store_name'] = store_name
        if vals:
            self.with_context(btcpay_skip_webhook_sync=True).write(vals)

    def _btcpay_check_webhook_exists(self, client):
        """Verify the stored webhook still exists server-side and points at us.

        :return: (exists: bool, message: str) — the message names the webhook
                 id (the endpoint URL is shown by the dedicated field).
        """
        self.ensure_one()
        if not self.btcpay_webhook_id:
            return False, _('No webhook registered yet.')
        if self.btcpay_webhook_id == 'manual':
            return True, _('Registered (manual webhook, created in the BTCPay UI).')
        try:
            hook = client.get_webhook(self.btcpay_webhook_id)
        except GreenfieldError as error:
            if error.status == 404:
                return False, _('The configured webhook no longer exists on the BTCPay store.')
            return False, str(error)
        endpoint_url = urls.url_join(self.get_base_url(), _WEBHOOK_PATH)
        if endpoint_url not in (hook.get('url') or ''):
            return False, _(
                'The webhook on the BTCPay store points to a different URL: %s',
                hook.get('url'),
            )
        return True, _('Registered (webhook id: %s).', hook.get('id'))

    def _btcpay_ensure_webhook(self, force=False):
        """Idempotent webhook registration (create only when needed).

        - existing webhook that still points at our endpoint: reused as-is
        - webhook deleted server-side (404) or ``force``: a fresh webhook is
          registered and the stale one (if any and deletable) removed
        - ``btcpay_webhook_id == 'manual'``: never touched automatically

        :return: (ok: bool, message: str)
        """
        self.ensure_one()
        if not self._btcpay_credentials_complete():
            return False, _('Credentials incomplete (URL, API key, store id).')
        client = self._btcpay_client()
        if not force and self.btcpay_webhook_id:
            exists, message = self._btcpay_check_webhook_exists(client)
            if exists:
                return True, message
        old_webhook_id = self.btcpay_webhook_id
        manual = old_webhook_id == 'manual'
        try:
            endpoint_url = urls.url_join(self.get_base_url(), _WEBHOOK_PATH)
            hook = client.create_webhook(endpoint_url, events=WEBHOOK_EVENTS)
        except GreenfieldError as error:
            return False, str(error)
        secret = hook.get('secret')
        if not secret:
            # A webhook without a secret cannot be verified; do not keep it.
            try:
                client.delete_webhook(hook.get('id'))
            except GreenfieldError:
                pass
            return False, _('BTCPay did not return a webhook secret.')
        if old_webhook_id and old_webhook_id != 'manual' and old_webhook_id != hook.get('id'):
            try:
                client.delete_webhook(old_webhook_id)
            except GreenfieldError as error:
                _logger.warning(
                    'BTCPay: could not delete stale webhook %s: %s', old_webhook_id, error)
        self.with_context(btcpay_skip_webhook_sync=True).write({
            'btcpay_webhook_id': hook.get('id'),
            'btcpay_webhook_secret': secret,
        })
        message = _('Registered (webhook id: %s).', hook.get('id'))
        if manual:
            message += _(' (the previous manual webhook must be removed in the BTCPay UI if still present)')
        return True, message

    # === BUTTON ACTIONS === #

    def action_btcpay_open_authorize(self):
        """Open the BTCPay api-keys/authorize page in a new browser tab.

        The callback (/payment/btcpay/authorize) receives the API key and
        applies the credentials atomically; the user is sent back here.
        Important: the provider record must be saved first so the id exists
        and the state token is meaningful.
        """
        self.ensure_one()
        if self.code != 'btcpayserver':
            raise UserError(_('This action is specific to the BTCPay provider.'))
        if not self.btcpay_location:
            raise UserError(_('Set the BTCPay Server URL first.'))
        url = self._btcpay_authorize_url()
        if not url:
            raise UserError(_('Could not build the BTCPay authorization URL.'))
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    def action_btcpay_test_connection(self):
        self.ensure_one()
        if self.code != 'btcpayserver':
            raise UserError(_('This action is specific to the BTCPay provider.'))
        if not self._btcpay_credentials_complete():
            raise UserError(_(
                'Set the BTCPay Server URL, an API key and a store id first.'))
        client = self._btcpay_client()
        try:
            store = client.get_store()
        except GreenfieldError as error:
            self._btcpay_update_status(connection=f'❌ {error}')
            raise UserError(_('BTCPay connection test failed: %s', error))
        if store.get('id') != self.btcpay_store_id:
            message = _(
                'The API key resolves to store %s, but store %s is configured.',
                store.get('id'), self.btcpay_store_id,
            )
            self._btcpay_update_status(
                connection=f'❌ {message}', store_name=store.get('name'))
            raise UserError(_('BTCPay connection test failed: %s', message))

        # Verify the API key's permissions and store scoping via the
        # current-api-key endpoint: it exposes the exact permission list,
        # where store-scoped permissions carry a ':<storeId>' suffix.
        try:
            key_info = client.get_current_api_key()
        except GreenfieldError as error:
            self._btcpay_update_status(connection=f'❌ {error}', store_name=store.get('name'))
            raise UserError(_('BTCPay connection test failed: %s', error))
        _ok, missing, warnings = client.check_permissions(self.btcpay_store_id)
        if missing:
            message = _(
                'The API key is missing permissions: %s. Create a new key with '
                'all six permissions listed in the API Key field help, bound to '
                'store %s.',
                ', '.join(missing), self.btcpay_store_id)
            self._btcpay_update_status(connection=f'❌ {message}', store_name=store.get('name'))
            raise UserError(_('BTCPay connection test failed: %s', message))
        if warnings:
            self._btcpay_update_status(
                connection=_('⚠️ Connected, but the API key is not scoped to the '
                             'store: %s (works server-wide). For least exposure '
                             'create a key bound to store %s.',
                             ', '.join(warnings), self.btcpay_store_id),
                store_name=store.get('name'))
            _logger.info(
                'BTCPay: API key %s grants server-wide permissions: %s',
                key_info.get('label'), ', '.join(warnings))
        else:
            self._btcpay_update_status(
                connection=_('✅ API key "%s" has all required permissions, '
                             'scoped to the store.', key_info.get('label')))

        exists, webhook_message = self._btcpay_check_webhook_exists(client)
        if not exists and self.btcpay_webhook_id:
            # Recover on the spot: register a fresh webhook.
            ok, webhook_message = self._btcpay_ensure_webhook(force=True)
            exists = ok
        self._btcpay_update_status(
            connection=f"✅ {_('Connected to store \"%s\"', store.get('name'))}",
            webhook=(f'✅ {webhook_message}' if exists else f'❌ {webhook_message}'),
            store_name=store.get('name'),
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('BTCPay'),
                'message': _('Connection OK: store "%s". %s', store.get('name'), webhook_message),
                'type': 'success' if exists else 'warning',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.client',
                    'tag': 'reload',
                },
            },
        }

    def action_btcpay_reregister_webhook(self):
        self.ensure_one()
        if self.code != 'btcpayserver':
            raise UserError(_('This action is specific to the BTCPay provider.'))
        ok, message = self._btcpay_ensure_webhook(force=True)
        self._btcpay_update_status(
            webhook=(f'✅ {message}' if ok else f'❌ {message}'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('BTCPay webhook'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': not ok,
                'next': {
                    'type': 'ir.actions.client',
                    'tag': 'reload',
                },
            },
        }

    def action_btcpay_disconnect(self):
        self.ensure_one()
        if self.code != 'btcpayserver':
            raise UserError(_('This action is specific to the BTCPay provider.'))
        warning = None
        if self.btcpay_webhook_id and self.btcpay_webhook_id != 'manual':
            try:
                self._btcpay_client().delete_webhook(self.btcpay_webhook_id)
                webhook_status = _('Webhook deleted on the BTCPay store.')
            except GreenfieldError as error:
                warning = _(
                    'The webhook %s could not be deleted on BTCPay (%s) - '
                    'remove it manually in the store settings.',
                    self.btcpay_webhook_id, error)
                webhook_status = f'⚠️ {warning}'
        else:
            webhook_status = _('No webhook was registered.')
        self.with_context(btcpay_skip_webhook_sync=True).write({
            'btcpay_api_key': False,
            'btcpay_store_id': False,
            'btcpay_webhook_secret': False,
            'btcpay_webhook_id': False,
            'btcpay_store_name': False,
            'btcpay_show_manual': False,
            'btcpay_connection_status': _(
                'Enter the BTCPay Server URL and click "Connect BTCPay Server", '
                'or enable Manual Setup and enter the credentials by hand.'),
            'btcpay_webhook_status': webhook_status,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('BTCPay'),
                'message': warning or _('Provider disconnected.'),
                'type': 'warning' if warning else 'success',
                'sticky': bool(warning),
                'next': {
                    'type': 'ir.actions.client',
                    'tag': 'reload',
                },
            },
        }

    # === OVERRIDES === #

    @api.model_create_multi
    def create(self, vals_list):
        providers = super().create(vals_list)
        if not self.env.context.get('btcpay_skip_webhook_sync'):
            for provider in providers.filtered(
                    lambda p: p.code == 'btcpayserver' and p._btcpay_credentials_complete()):
                ok, message = provider._btcpay_ensure_webhook()
                provider._btcpay_update_status(
                    webhook=(f'✅ {message}' if ok else f'❌ {message}'))
        return providers

    def write(self, vals):
        res = super().write(vals)
        credential_change = {'btcpay_location', 'btcpay_api_key', 'btcpay_store_id'} & set(vals)
        if credential_change and not self.env.context.get('btcpay_skip_webhook_sync'):
            for provider in self.filtered(
                    lambda p: p.code == 'btcpayserver' and p._btcpay_credentials_complete()):
                ok, message = provider._btcpay_ensure_webhook()
                provider._btcpay_update_status(
                    webhook=(f'✅ {message}' if ok else f'❌ {message}'))
        return res
