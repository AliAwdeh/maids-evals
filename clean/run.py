import json
import csv

with open("test.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# If the JSON is a single object, wrap it in a list
if isinstance(data, dict):
    data = [data]

with open("test.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=data[0].keys())
    writer.writeheader()
    writer.writerows(data)
