from agent import pending_store

pending_store.add_pending({
    "order_id": "order_TYFAFz4eHgLgx9",
    "payment_id": "pay_TWICZl2eqzh1jM",
    "amount": 499.0,
})

print("Registered as pending.")

import json
print(json.dumps(pending_store.list_pending(), indent=2, default=str))
