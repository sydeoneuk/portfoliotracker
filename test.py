import requests

#url = "https://live.trading212.com/api/v0/equity/orders"

#response = requests.get(url, auth=('40892910ZkFRErfxvOqEXsRYyEmZLwsvUoaJJ','_fh-j4icloFQD1fvAydOJyZUZsfMNWKpHveRrbcQMIM'))

#data = response.json()
#print(data)




url = "https://live.trading212.com/api/v0/equity/pies"

payload = {
  "dividendCashAction": "REINVEST",
  "endDate": "2030-08-24T14:15:22Z",
  "goal": 0,
  "icon": "string",
  "instrumentShares": {
    "AAPL_US_EQ": 0.5,
    "MSFT_US_EQ": 0.5
  },
  "name": "API Pie"
}

headers = {"Content-Type": "application/json"}

response = requests.post(url, json=payload, headers=headers, auth=('40892910ZSWWGTDKeveJCuwOjSPJfTnmPLjaj','JMJdSXVT7GFOD8FJx5drgoCvA2ZnFwoD6G4E5sjIknU'))

print(response)

data = response.json()
print(data)