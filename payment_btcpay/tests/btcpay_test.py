import pprint
import logging
import requests
import json
import re
import os.path
#import bitpay_key_utils as bku
#from bitpay_client import *

import btcpay.crypto
from btcpay import BTCPayClient

API_HOST = "https://testnet.demo.btcpayserver.org" #Testnet BTCPAY
KEY_FILE = "/tmp/key.priv"
TOKEN_FILE = "/tmp/btctoken.priv"

if os.path.isfile(KEY_FILE):
	f = open(KEY_FILE, 'r')
	key = f.read()
	f.close()
	print("Creating a bitpay client using existing private key from disk.")
else:
	key = btcpay.crypto.generate_privkey()
	f = open(KEY_FILE, 'w')
	f.write(key)
	f.close()

client = BTCPayClient(host=API_HOST, pem=key)


def fetch_token(facade, pairingCode):
	if os.path.isfile(TOKEN_FILE + facade):
		f = open(TOKEN_FILE + facade, 'r')
		token = f.read()
		f.close()
		client.tokens[facade] = token
	else:
		token = client.pair_client(pairingCode)
		client.tokens[facade] = token
		f = open(TOKEN_FILE + facade, 'w')
		print(token[facade])
		f.write(token[facade])
		f.close()

pairingCode = "otMSapf"
facade = "merchant"
fetch_token(facade, pairingCode)
print(client.tokens[facade])

client = BTCPayClient(host=API_HOST, pem=key, tokens=client.tokens)
print(client)
#fetch_token(facade, pairingCode)
#print("Token: ", client.tokens[facade])
new_invoice = client.create_invoice({"price": 20, "currency": "EUR", "token": client.tokens[facade],"extendedNotifications": True})
print("NEW INVOICE: ", new_invoice)

fetched_invoice = client.get_invoice(new_invoice['id'])
print("FETCHED INVOICE: ", new_invoice['id'])


curl --no-keepalive --raw --show-error --verbose --connect-timeout 10 --insecure --max-redirs 1 -H "Content-Type: application/json" -d '{"data": "{"id":"8vQMp1C6gB1DxUyJB9gcBp"}"}" http://odoo-dev.zynthian.org/payment/btcpay/ipn