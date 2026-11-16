from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.payment.logging import get_payment_logger

from .libs.greenfield_client import GreenfieldClient, GreenfieldError, WEBHOOK_EVENTS

_logger = get_payment_logger(__name__)

_WEBHOOK_PATH = '/payment/btcpay/webhook'


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

    # === COMPUTED === #

    def _compute_btcpay_webhook_url(self):
        for provider in self:
            try:
                base_url = provider.get_base_url()
            except Exception:
                base_url = ''
            provider.btcpay_webhook_url = urls.url_join(base_url, _WEBHOOK_PATH) if base_url else False

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

        :return: (exists: bool, message: str)
        """
        self.ensure_one()
        if not self.btcpay_webhook_id:
            return False, _('No webhook registered yet.')
        if self.btcpay_webhook_id == 'manual':
            return True, _('Manual webhook (created in the BTCPay UI).')
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
        return True, _('Registered: %s', hook.get('url'))

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
        message = _('Registered: %s', endpoint_url)
        if manual:
            message += _(' (the previous manual webhook must be removed in the BTCPay UI if still present)')
        return True, message

    # === BUTTON ACTIONS === #

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
            'btcpay_connection_status': _(
                'Disconnected. Re-enter the credentials to connect again.'),
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
