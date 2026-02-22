
WITH tx AS (
  SELECT
    block_time,
    bytearray_to_uint256(bytearray_substring(data, 5, 32)) / 1e18 AS amount
  FROM base.transactions
  WHERE
    to = 0x321b7ff75154472B18EDb199033fF4D116F340Ff
    AND bytearray_substring(data, 1, 4) = 0xae5ac921
    AND block_time >= now() - interval '30' day
),

_days AS (
  SELECT day
  FROM unnest(sequence(
    CAST(date_trunc('day', now() - interval '30' day) AS date),
    CAST(date_trunc('day', now()) AS date),
    interval '1' day
  )) AS t(day)
),

initiated AS (
  SELECT CAST(date_trunc('day', block_time) AS date) AS day, SUM(amount) AS initiated_amount
  FROM tx
  GROUP BY 1
),

unlocks AS (
  SELECT CAST(date_trunc('day', block_time + interval '7' day) AS date) AS day, SUM(amount) AS unlock_amount
  FROM tx
  GROUP BY 1
),

queue AS (
  SELECT d.day,
    SUM(t.amount) AS queue_amount
  FROM _days d
  LEFT JOIN tx t
    ON t.block_time > CAST(d.day AS timestamp) - interval '7' day
   AND t.block_time <= CAST(d.day AS timestamp) + interval '1' day
  GROUP BY 1
)

SELECT
  d.day,
  COALESCE(i.initiated_amount, 0) AS initiated_amount,
  COALESCE(u.unlock_amount, 0) AS unlock_amount,
  COALESCE(q.queue_amount, 0) AS queue_amount
FROM _days d
LEFT JOIN initiated i ON d.day = i.day
LEFT JOIN unlocks u ON d.day = u.day
LEFT JOIN queue q ON d.day = q.day
ORDER BY d.day;


-- summary


WITH tx AS (
  SELECT
    block_time,
    bytearray_to_uint256(bytearray_substring(data, 5, 32)) / 1e18 AS amount
  FROM base.transactions
  WHERE
    to = 0x321b7ff75154472B18EDb199033fF4D116F340Ff
    AND bytearray_substring(data, 1, 4) = 0xae5ac921
    AND block_time >= now() - interval '30' day
),

_days AS (
  SELECT day
  FROM unnest(sequence(
    CAST(date_trunc('day', now() - interval '30' day) AS date),
    CAST(date_trunc('day', now()) AS date),
    interval '1' day
  )) AS t(day)
),

queue AS (
  SELECT d.day,
    SUM(t.amount) AS queue_amount
  FROM _days d
  LEFT JOIN tx t
    ON t.block_time > CAST(d.day AS timestamp) - interval '7' day
   AND t.block_time <= CAST(d.day AS timestamp) + interval '1' day
  GROUP BY 1
),

daily_initiated AS (
  SELECT CAST(date_trunc('day', block_time) AS date) AS day, SUM(amount) AS initiated_amount
  FROM tx
  GROUP BY 1
)

SELECT
  (SELECT queue_amount FROM queue ORDER BY day DESC LIMIT 1) AS current_queue_amount,
  (SELECT AVG(queue_amount) FROM queue) AS avg_queue_amount_30d,
  (SELECT AVG(initiated_amount) FROM daily_initiated) AS avg_daily_initiated_30d,
  (SELECT COALESCE(SUM(amount),0) FROM tx WHERE block_time > now() - interval '7' day) AS initiated_last_7d
