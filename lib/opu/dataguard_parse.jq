# Parse only the tagged rows emitted by opu-dataguard-observe's fixed SQL.
# Any unexpected SQL*Plus diagnostics, duplicate rows, invalid identifiers,
# intervals, units or timestamps abort collection instead of becoming zero lag.
def trim: gsub("^\\s+|\\s+$"; "");
def maybe: if . == "~" or . == "" then null else . end;
def db_name:
  if test("^[A-Za-z0-9][A-Za-z0-9._$#-]{0,127}$") then .
  else error("invalid native DB_UNIQUE_NAME") end;
def local_epoch:
  . as $value |
  (if test("^[0-9]{2}/[0-9]{2}/[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}$") then "%m/%d/%Y %H:%M:%S"
   elif test("^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$") then "%Y-%m-%d %H:%M:%S"
   else error("unsupported Data Guard local timestamp format") end) as $format |
  ($value | strptime($format) | mktime) as $epoch |
  if ($epoch | strftime($format)) == $value then $epoch
  else error("invalid Data Guard local timestamp") end;
def interval_seconds:
  if test("^\\+?[0-9]+ [0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?$") then
    capture("^\\+?(?<d>[0-9]+) (?<h>[0-9]{2}):(?<m>[0-9]{2}):(?<s>[0-9]{2}(\\.[0-9]+)?)$") |
    with_entries(.value |= tonumber) |
    if .h < 24 and .m < 60 and .s < 60 then (.d*86400 + .h*3600 + .m*60 + .s | ceil)
    else error("invalid Data Guard lag interval fields") end
  else error("invalid Data Guard lag interval") end;
def metric($rows; $name; $clock):
  [$rows[] | select(.[0] == "DG_METRIC" and .[1] == $name)] as $matches |
  if ($matches | length) > 1 then error("duplicate Data Guard lag metric")
  elif ($matches | length) == 0 then {status:"unavailable",name:$name,seconds:-1}
  else $matches[0] as $row |
    ($row[2] | maybe) as $value | ($row[3] | maybe) as $unit |
    ($row[4] | maybe) as $computed | ($row[5] | maybe) as $datum |
    {name:$name,value:$value,unit:$unit,time_computed:$computed,datum_time:$datum,
      source_db_unique_name:($row[6] | maybe),clock:"database_local"} +
    (if $value == null or $unit == null or $computed == null or $datum == null or ($row[6] | maybe) == null then
       {status:"unavailable",seconds:-1}
     elif ($unit | test("^day\\([0-9]+\\) to second\\([0-9]+\\) interval$")) | not then
       error("unsupported Data Guard lag unit")
     else {status:"collected",seconds:($value | interval_seconds),
       time_computed_age_seconds:($clock - ($computed | local_epoch)),
       datum_age_seconds:($clock - ($datum | local_epoch))} end)
  end;

split("\n") | map(gsub("\r"; "") | trim | select(length > 0) | split("|") | map(trim)) as $rows |
if $mode == "identity" then
  if ($rows | length) != 1 or ($rows[0] | length) != 8 or $rows[0][0] != "DG_DATABASE" then
    error("expected exactly one tagged V$DATABASE row")
  else $rows[0] as $row |
    {database_role:$row[1],open_mode:$row[2],protection_mode:$row[3],db_unique_name:($row[4] | db_name),
     primary_db_unique_name:($row[5] | maybe),sampled_at_local:$row[6],dataguard_broker:$row[7]} |
    if (.database_role | IN("PRIMARY","PHYSICAL STANDBY","LOGICAL STANDBY","SNAPSHOT STANDBY","FAR SYNC")) and
       (.open_mode | IN("MOUNTED","READ WRITE","READ ONLY","READ ONLY WITH APPLY")) and
       (.protection_mode | length > 0) and (.sampled_at_local | local_epoch | type == "number")
    then . else error("invalid native database metadata") end
  end
elif $mode == "standby" then
  if any($rows[]; (.[0] == "DG_CLOCK" and length != 2) or
    (.[0] == "DG_APPLY" and length != 3) or (.[0] == "DG_METRIC" and length != 7) or
    ((.[0] | IN("DG_CLOCK","DG_APPLY","DG_METRIC")) | not)) then error("unexpected standby SQL output")
  else
    [$rows[] | select(.[0] == "DG_CLOCK")] as $clocks |
    [$rows[] | select(.[0] == "DG_APPLY")] as $processes |
    if ($clocks | length) != 1 or ($processes | length) > 1 then error("ambiguous standby clock/apply process")
    else ($clocks[0][1] | local_epoch) as $clock |
      metric($rows; "transport lag"; $clock) as $transport |
      metric($rows; "apply lag"; $clock) as $apply |
      if any([$transport,$apply][]; .source_db_unique_name != null and
         (.source_db_unique_name != $identity.primary_db_unique_name)) then
        error("lag metric source does not match native primary identity")
      else {sampled_at_local:$clocks[0][1],members:[{
        db_unique_name:$identity.db_unique_name,
        status:($processes[0][2] // "unknown"),
        apply_process:($processes[0][1] // null),
        transport_lag_seconds:$transport.seconds,apply_lag_seconds:$apply.seconds,
        lag_source:"v$dataguard_stats",lag_samples:{transport:$transport,apply:$apply}
      }]} end
    end
  end
elif $mode == "primary" then
  if any($rows[]; .[0] != "DG_DEST" or length != 6) then error("unexpected primary destination SQL output")
  else
    {sampled_at_local:$identity.sampled_at_local,members:($rows | map({
      db_unique_name:(.[1] | db_name),status:.[2],recovery_mode:.[3],destination_type:.[4],error:(.[5] | maybe),
      transport_lag_seconds:-1,apply_lag_seconds:-1,lag_source:"unavailable_on_primary",
      lag_samples:{transport:{status:"unavailable",seconds:-1},apply:{status:"unavailable",seconds:-1}}
    }))} |
    if (.members | map(.db_unique_name) | length) == (.members | map(.db_unique_name) | unique | length)
    then . else error("ambiguous duplicate standby destinations") end
  end
else error("unknown Data Guard parser mode") end
