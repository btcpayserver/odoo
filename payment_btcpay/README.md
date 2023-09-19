# payment_btcpay
# Gateway to BTCPay for Odoo 16.0

## This is the module to connect Odoo 16.0 and BTCPay
This module allow you to create an easily way to accept cryptocurrencies.
![BTCPay](/payment_btcpay/static/description/Btcpay_com.png)
  
## Configure Payment Acquirer
* Install BTCPay Module -> Website -> eCommerce -> Payment Acquirers -> BTCPay
* Put your facace. Best option is 'merchant'.
* Put the location as test or live url. Test example url: https://testnet.demo.btcpayserver.org
* Put the Confirmation URL where BTCpay will return after payment.
* Check if you want that Odoo send an email to your buyer after transaction is "Confirmed"
* Put your "Pairing Code" if you want that system get the "Token", after that "Pairing Code" will be deleted and the "Token" will appear in the corresponding field. You must safe the changes in order that this happens. NOTE: if you want to get new "Token" throw new "Pairing Code", please remove the "Token" field. Keep in mind that "Token" field must be in blank if you want to get throw the API.
* Put your "Token" if you have it and don't want to use Pairing Code to get it.  Remember, if you want to get throw API please don't write anything here.
* If you have a Private Key you can write here otherwise system will get when you safe the Payment Acquirer
* Remember to Publish On Website

![Payment Acquirer](/payment_btcpay/static/description/BTCPayPaymentAcquirer.png)

## How it looks like?

In payment webpage where payment methods appear, you will find new payment method called BTCPay. If you click on it you will be redirect to the server that you indicate in location field.

![Payment Acquirer](/payment_btcpay/static/description/BTCPayLooksLike.png)


## Transaction BTCPay Details
In transaction object, you will find more technical information about this method of payment:
* Transaction Id: cryptocurrency transaction hash for the executed payout
* Invoice Id: the id of the invoice for which you want to fetch an event token
* Transaction Status: That indicates state of transaction

![Transaction Btcpay Details](/payment_btcpay/static/description/BtcpayTxDetails.png)
