"""Durable, at-least-once delivery of real control-plane logs and spans via OTLP/HTTP JSON."""
import json
import urllib.error
import urllib.request

def attr(key, value):
    return {"key":key,"value":{"stringValue":str(value)}}

def deliver(engine, endpoint, limit=100):
    """Acknowledge logs and spans separately; a crash can redeliver either.

    Stable sequence/span IDs support investigation of duplicates. This adapter
    does not claim exactly-once export or guaranteed backend durability.
    """
    with engine._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT e.*,o.logs_sent,o.traces_sent FROM events e JOIN outbox o USING(sequence) WHERE o.logs_sent=0 OR o.traces_sent=0 ORDER BY sequence LIMIT ?",(limit,))]
    sent,failed = 0,0
    for e in rows:
        resource = {"attributes":[attr("service.name","fleet-reliability-lab"),attr("tenant.id",e["tenant"]),attr("telemetry.origin","synthetic-hardware-real-control-plane")]}
        attributes = [attr("event.sequence",e["sequence"]),attr("event.kind",e["kind"]),attr("asset.id",e["asset_id"] or ""),attr("incident.id",e["incident_id"] or "")]
        ns = int(e["ingested_at"]*1_000_000_000)
        channels = []
        if not e["logs_sent"]:
            payload = {"resourceLogs":[{"resource":resource,"scopeLogs":[{"scope":{"name":"fleetlab.events"},"logRecords":[{"timeUnixNano":str(ns),"observedTimeUnixNano":str(ns),"severityNumber":9,"severityText":"INFO","body":{"stringValue":json.dumps({"kind":e["kind"],"data":json.loads(e["data"])},sort_keys=True)},"attributes":attributes,"traceId":e["trace_id"],"spanId":e["span_id"]}]}]}]}
            channels.append(("logs",payload))
        if not e["traces_sent"]:
            # Transition spans are event-derived; timing is not a GPU benchmark.
            data = json.loads(e["data"])
            duration_ns = max(1,int(float(data.get("duration_ms",0))*1_000_000))
            span = {"traceId":e["trace_id"],"spanId":e["span_id"],"name":e["kind"],"kind":1,"startTimeUnixNano":str(ns-duration_ns),"endTimeUnixNano":str(ns),"attributes":attributes+[attr("span.origin","event-derived control-plane transition")],"status":{"code":1}}
            payload = {"resourceSpans":[{"resource":resource,"scopeSpans":[{"scope":{"name":"fleetlab.events"},"spans":[span]}]}]}
            channels.append(("traces",payload))
        for channel,payload in channels:
            try:
                request = urllib.request.Request(endpoint.rstrip('/')+"/v1/"+channel, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
                with urllib.request.urlopen(request,timeout=5) as response:
                    body = response.read()
                    result = json.loads(body) if body else {}
                    partial = result.get("partialSuccess",{})
                    if partial.get("rejectedLogRecords",0) or partial.get("rejectedSpans",0):
                        raise RuntimeError("OTLP destination partially rejected records")
                with engine.tx() as db:
                    db.execute(f"UPDATE outbox SET {channel}_sent=1,last_error=NULL WHERE sequence=?",(e["sequence"],))
                sent += 1
            except (OSError,ValueError,RuntimeError) as error:
                with engine.tx() as db:
                    db.execute("UPDATE outbox SET attempts=attempts+1,last_error=? WHERE sequence=?",(str(error)[:300],e["sequence"]))
                failed += 1
    return {"delivered_channels":sent,"failed_channels":failed,"events_considered":len(rows)}
