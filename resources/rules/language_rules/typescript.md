### Context: typescript

TypeScript's guarantees end wherever the type system is escaped. Most real defects in reviewed diffs enter through `any`, a non-null assertion, an unchecked cast, or an unhandled promise. Weigh those above stylistic concerns.

### Context: ts-type-escape

An escape from the type system is only a defect when something goes wrong
because of it. `any` on a callback parameter in a file that never misuses the
value costs nothing at runtime; a non-null assertion on a value a readable path
leaves undefined crashes. Severity follows that difference, not the syntax.

### Criteria: ts-type-escape

- A non-null assertion (`!`) added on a value that a readable path can leave `null` or `undefined` MUST be flagged at `error`, naming the path that produces the absent value.
- `any` or an unchecked cast through which a value of a *different* shape can actually reach a consumer MUST be flagged at `warning`, naming the consumer and what breaks when it arrives.
- `any` introduced where the actual type is known or inferrable, with no such consumer, MAY be flagged at `info`, and SHOULD NOT be flagged when the same file already carries a more pressing finding. A bare "this is `any`" restates the diff.
- `as unknown as T` MUST be flagged at `warning` only when the two types are shown to be incompatible; otherwise it falls under the `info` clause above.

### Criteria: ts-async

- A promise created and neither awaited nor returned nor explicitly handled with `.catch` MUST be flagged at `error`. The rejection becomes an unhandled rejection.
- `await` inside a loop where the iterations are independent SHOULD be flagged at `info` under the performance category.
- An `async` function passed where a synchronous callback is expected (for example, `Array.prototype.forEach`) MUST be flagged at `error`; the caller cannot observe completion or failure.

### Criteria: ts-react-hooks

- A `useEffect`, `useMemo`, or `useCallback` whose dependency array omits a value the body reads MUST be flagged at `warning`; a stale value is captured.
- An effect that subscribes, opens a timer, or adds a listener without returning a cleanup function MUST be flagged at `error`.
- State updated from a value derived from the previous state without the updater form SHOULD be flagged at `warning`.

### Criteria: ts-correctness

- `==` used with a value that can be `0`, `""`, or `NaN`, where the intent is an existence check, MUST be flagged at `warning`.
- Object or array spread used to "clone" a structure whose nested members are then mutated MUST be flagged at `warning`; the copy is shallow.
- User-controlled data assigned to `innerHTML`, `dangerouslySetInnerHTML`, or a URL used as a script source MUST be flagged at `error` under the security category.

### Criteria: ts-i18n

- A user-facing string literal added to a component that elsewhere routes strings through a translation function MUST be flagged at `info`.

### Directive: ts-evidence

- When flagging a stale closure or missing dependency, name the value that goes stale and the render in which it is read.
