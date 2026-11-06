"""Set the kibana_system password so Kibana can connect. Runs once before Kibana starts."""

import requests

from common import env, wait_for

ES = "http://elasticsearch:9200"
auth = ("elastic", env("ELASTIC_PASSWORD", required=True))

wait_for("Elasticsearch", lambda: requests.get(ES, auth=auth, timeout=5).ok)
resp = requests.post(f"{ES}/_security/user/kibana_system/_password", auth=auth, timeout=30,
                     json={"password": env("KIBANA_PASSWORD", required=True)})
resp.raise_for_status()
print("kibana_system password set")
