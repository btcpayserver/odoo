import requests
import json

url = 'https://odoo-dev.zynthian.org/payment/btcpay/ipn'
headers = {'Content-Type': 'application/json'}
data = {
    "data": {
        "id":"8vQMp1C6gB1DxUyJB9gcBp",
    }
}
data_json = json.dumps(data)
r = requests.post(url=url, data=data_json, headers=headers)