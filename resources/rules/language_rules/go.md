### Context: go

Go's failure modes are quiet: an ignored error, a `defer` in the wrong scope, a goroutine that outlives its caller. None of these fail to compile, and none announce themselves at runtime until load. Prefer these checks over style commentary, which `gofmt` and `go vet` already cover.

### Criteria: go-errors

- An error assigned to `_`, or a call returning an error whose result is discarded, MUST be flagged at `error` unless the call cannot fail.
- An error wrapped and returned that loses the original through `fmt.Errorf` without `%w` SHOULD be flagged at `info`.
- A code path that logs an error and continues as though the operation succeeded MUST be flagged at `warning`.

### Criteria: go-nil

- A pointer, map, or interface dereferenced after a call that a readable path can leave `nil` MUST be flagged at `error` when the path is visible.
- A write to a `nil` map MUST be flagged at `error`.
- An error checked as `err != nil` after a call that returns a typed nil pointer in the error position MUST be flagged at `warning`.

### Criteria: go-defer

- A `defer` placed inside a loop where the enclosing function does not return per iteration MUST be flagged at `error`; releases accumulate until the function exits.
- A `defer` registered before its corresponding error check, so it runs on a resource that was never acquired, MUST be flagged at `error`.
- A response body, file, or lock acquired with no matching `defer` release on every exit path MUST be flagged at `error`.

### Criteria: go-concurrency

- A goroutine started without a mechanism that lets the caller observe completion or cancellation MUST be flagged at `warning`.
- A loop variable captured by a goroutine started inside the loop MUST be flagged at `error` for Go versions before 1.22, and at `info` otherwise.
- Shared state written from a goroutine and read elsewhere without a mutex, channel, or atomic MUST be flagged at `error`.
- A `context.Context` accepted by a function but not forwarded to the calls it makes SHOULD be flagged at `warning`; cancellation stops propagating.

### Criteria: go-slices

- A slice retained after being produced by `append` on a caller-owned backing array MUST be flagged at `warning`; the two aliases share storage.
- A subslice of a large buffer retained beyond the buffer's intended lifetime SHOULD be flagged at `info` under the performance category.

### Directive: go-evidence

- When flagging a leaked goroutine or missing release, name the exit path on which the release does not run.
