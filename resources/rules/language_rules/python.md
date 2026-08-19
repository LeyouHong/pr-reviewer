### Context: python

Python defers most type and attribute errors to runtime, so a defect that another language would fail to compile reaches production here. Prefer flagging a concrete failing input over flagging a style deviation.

### Criteria: python-mutable-default

- A function or method whose default argument is a mutable literal (`[]`, `{}`, `set()`) or a call evaluated at definition time MUST be flagged at `error`. The object is shared across every call.

### Criteria: python-optional-flow

- An added expression that attributes or subscripts a value which a readable code path can set to `None` MUST be flagged at `warning`, and at `error` when the `None`-producing path is visible in the diff.
- `dict.get(...)` results consumed without a guard fall under this criteria.

### Criteria: python-exceptions

- A bare `except:` or `except Exception:` whose body neither re-raises nor logs MUST be flagged at `error`. Silently swallowing an exception hides the failure it was meant to surface.
- An added `except` clause that catches a broader type than the operation can raise SHOULD be flagged at `warning`.

### Criteria: python-resources

- A file, socket, connection, lock, or subprocess acquired outside a `with` block, where an added code path can leave the scope by exception or early return before the release call, MUST be flagged at `error`.

### Criteria: python-concurrency

- Shared mutable state read and written from more than one thread or task without a lock MUST be flagged at `warning`.
- A blocking call (`time.sleep`, synchronous I/O, CPU-bound loop) added inside an `async def` MUST be flagged at `error`. It stalls the event loop.
- An awaitable created and never awaited MUST be flagged at `error`.

### Criteria: python-correctness

- `==` used to compare against `None`, `True`, or `False` SHOULD be flagged at `info`; `is` is the correct operator and the difference is observable for objects overriding `__eq__`.
- A loop variable captured by a closure defined inside the loop MUST be flagged at `warning`; every closure observes the final value.
- String formatting of user-controlled data into SQL, shell, or path arguments MUST be flagged at `error` under the security category.

### Directive: python-evidence

- When flagging a `None` dereference, name the code path that produces `None`. A type annotation permitting `Optional` is not by itself evidence.
