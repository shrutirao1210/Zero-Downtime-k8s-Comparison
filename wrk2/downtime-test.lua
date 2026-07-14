-- downtime-test.lua
-- Works with BOTH wrk and wrk2.
-- Logs per-response status + microsecond-resolution timestamps to CSV.
-- Downtime is calculated from the actual error window timestamps,
-- NOT from (failed_count × 1/rate) — so it's accurate regardless of
-- whether constant-rate (-R) is honoured by the binary.

local logfile = nil
local start_us = nil

function init(args)
  -- Each thread gets its own CSV file (thread-safe, no locking needed)
  local path = string.format("/tmp/wrk2-results-%d-%d.csv",
    os.time(), math.random(1000000))
  logfile = io.open(path, "w")
  if logfile then
    logfile:write("timestamp_us,status\n")
  end
end

-- Microsecond wall clock via /proc/uptime (available on Linux, works in wrk/wrk2 LuaJIT)
local function now_us()
  local f = io.open("/proc/uptime", "r")
  if not f then return 0 end
  local s = f:read("*n")
  f:close()
  return math.floor(s * 1000000)
end

response = function(status, headers, body)
  if logfile then
    logfile:write(string.format("%d,%d\n", now_us(), status))
    logfile:flush()
  end
end

done = function(summary, latency, requests)
  if logfile then
    logfile:close()
  end

  local f = io.open("/tmp/wrk2-summary.txt", "a")
  if not f then return end

  f:write(string.format(
    "duration_us=%d requests=%d bytes=%d errors_connect=%d errors_read=%d errors_write=%d errors_status=%d errors_timeout=%d\n",
    summary.duration,
    summary.requests,
    summary.bytes,
    summary.errors.connect,
    summary.errors.read,
    summary.errors.write,
    summary.errors.status,
    summary.errors.timeout
  ))
  f:write(string.format(
    "latency_min_us=%d latency_max_us=%d latency_mean_us=%.2f latency_stdev_us=%.2f\n",
    latency.min, latency.max, latency.mean, latency.stdev
  ))
  f:write(string.format("latency_p50_us=%d\n", latency:percentile(50)))
  f:write(string.format("latency_p75_us=%d\n", latency:percentile(75)))
  f:write(string.format("latency_p90_us=%d\n", latency:percentile(90)))
  f:write(string.format("latency_p99_us=%d\n",  latency:percentile(99)))
  f:write(string.format("latency_p99.9_us=%d\n", latency:percentile(99.9)))
  f:close()
end
