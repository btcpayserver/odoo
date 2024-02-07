# payment_btcpay
# BTCPay Server payment gateway for Odoo 16.0

## This is the module to connect Odoo 16.0 and BTCPay
This module allows you to accept bitcoin (and other cryptocurrency) payments in your Odoo e-commerce store.  
![BTCPay](/payment_btcpay/static/description/Btcpay_com.png)

## Install the module
* Clone our [repository](https://github.com/btcpayserver/odoo) or download the .zip from the [releases page](https://github.com/btcpayserver/odoo/releases
* Place the `payment_btcpay` directory in your Odoo addons directory
* Install dependencies by running `pip install -r requirements.txt`
* Restart Odoo
* Go to Apps -> Update Apps List
* Remove the "Apps" filter and search for "btcpay"
* Click **Activate** button
  
## Configure BTCPay as payment provider
* Go to  **Website** -> **Configuration** -> **Payment Providers**
* Search for BTCPay and click on button **Activate**

* State, set to enabled
* Put the location as test or live url. Test example url: https://testnet.demo.btcpayserver.org
* Put the Confirmation URL where BTCPay will return after payment.
* Fields Token and Private key, leave empty
* Field Facade, keep default 'merchant'.
* * Check if you want that Odoo send an email to your buyer after transaction is "Confirmed"
* Put your "Pairing Code" if you want that system get the "Token", after that "Pairing Code" will be deleted and the "Token" will appear in the corresponding field. You must safe the changes in order that this happens. NOTE: if you want to get new "Token" throw new "Pairing Code", please remove the "Token" field. Keep in mind that "Token" field must be in blank if you want to get throw the API.
* After you wrote the pairing code the Token and Private key will be filled automatically if the pairing was successful


![Payment Provider](/payment_btcpay/static/description/BTCPayPaymentProvider.png)

## How it looks like?

In payment webpage where payment methods appear, you will find new payment method called BTCPay. If you click on it you will be redirect to the server that you indicate in location field.

![Payment Provider](/payment_btcpay/static/description/BTCPayLooksLike.png)


## Transaction BTCPay Details
In transaction object, you will find more technical information about this method of payment:
* Transaction Id: cryptocurrency transaction hash for the executed payout
* Invoice Id: the id of the invoice for which you want to fetch an event token
* Transaction Status: That indicates state of transaction

![Transaction Btcpay Details](/payment_btcpay/static/description/BtcpayTxDetails.png)
