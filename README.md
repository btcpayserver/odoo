# BTCPay Server payment gateway for Odoo 16 - 20

## Accept bitcoin payments on Odoo 16, 17, 18, 19 and 20
This odoo addon allows you to accept bitcoin and Lightning Network (and other cryptocurrency) payments in your Odoo e-commerce store.  
![BTCPay Server Banner](https://raw.githubusercontent.com/btcpayserver/odoo/20.0/payment_btcpayserver/static/description/BTCPay-Odoo-17-featured.png)

:::warning
**Odoo 20 version guidance:** release
[v20.0.1.0](https://github.com/btcpayserver/odoo/releases/tag/v20.0.1.0) still
uses the **legacy BitPay API** with pairing codes (same setup as Odoo 18/19 —
no reconnect needed when upgrading from v1.x). From release
**v20.0.2.0** on, the branch uses the **BTCPay Greenfield API** (API key +
store id + signed webhooks) — a breaking change for existing v1.x users, who
need to reconnect the provider once after upgrading. Branches `18.0`/`19.0`
stay on the legacy BitPay API; `16.0`/`17.0` are end-of-life.
:::

## How to install

Please find the installation instructions here:
- [Odoo 20 documentation (this branch, Greenfield API, setup wizard)](https://github.com/btcpayserver/odoo/blob/20.0/payment_btcpayserver/README.md)
- [Odoo 18 + 19 + 20 v1.x documentation (legacy BitPay API)](https://github.com/btcpayserver/odoo/blob/90e455173a0f517f138789a2ca793cebca0bc746/payment_btcpayserver/README.md)
- [Odoo 18 + 19 documentation (current legacy branch)](https://github.com/btcpayserver/odoo/blob/19.0/payment_btcpayserver/README.md)
- [Odoo 17 documentation](https://github.com/btcpayserver/odoo/blob/17.0/payment_btcpayserver/README.md)
- [Odoo 16 documentation](https://github.com/btcpayserver/odoo/blob/16.0/payment_btcpayserver/README.md)
