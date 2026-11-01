# Orders behavior

1. Creating an order with a nonempty title of at most 100 characters allocates one
   UUID and returns status CREATED. The UI displays that title, ID, and status.
2. Cancelling a CREATED order changes that same order to CANCELLED. The UI and API
   must show the new state. Cancellation does not create another order.
3. Repeating an API action with the same idempotency key does not allocate a second
   order or apply a distinct action. A new key is a new delivery, not proof of safety.
4. Missing records return 404. Invalid titles or missing idempotency keys return 400.

These requirements supply expectations independently of an observed UI or response.
The demo's optional defect mode intentionally violates the UI part of requirement 2;
the execution runtime does not know or evaluate that business rule.
