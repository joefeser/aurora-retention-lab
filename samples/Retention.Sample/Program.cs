using Microsoft.EntityFrameworkCore;
using Npgsql;
using System.Text.Json;

var connectionString = Environment.GetEnvironmentVariable("RETENTION_SAMPLE_CONNECTION")
    ?? throw new InvalidOperationException("Use scripts/run-ef-sample.py to provision the disposable local database.");
var cs = new NpgsqlConnectionStringBuilder(connectionString);
if (cs.Host != "127.0.0.1" || cs.Database != "retention_sample")
    throw new InvalidOperationException("Only the loopback retention_sample database is permitted.");
var output = args.Single();
var checks = new List<string>();
var probes = new List<object>();
var capture = new SqlCapture();
var passed = false;
string? engine = null;
await using var admin = new NpgsqlConnection(connectionString);
await admin.OpenAsync();
async Task<object?> Scalar(NpgsqlConnection c, string sql) => await new NpgsqlCommand(sql, c).ExecuteScalarAsync();
async Task Sql(NpgsqlConnection c, string sql) => await new NpgsqlCommand(sql, c).ExecuteNonQueryAsync();
void Require(bool ok, string label)
{
    if (!ok) throw new InvalidOperationException(label);
    checks.Add(label);
}
SampleContext Context(bool partitioned, NpgsqlDataSource source)
{
    var options = new DbContextOptionsBuilder().UseNpgsql(source).AddInterceptors(capture).Options;
    return partitioned ? new PartitionContext(options) : new BaselineContext(options);
}
NpgsqlDataSource Source(string mode = "auto", bool prepare = false)
{
    var b = new NpgsqlConnectionStringBuilder(connectionString) {
        MaxPoolSize = 1, MaxAutoPrepare = prepare ? 50 : 0, AutoPrepareMinUsages = 2,
        Options = $"-c plan_cache_mode={mode} -c lock_timeout=500ms -c statement_timeout=5s"
    };
    return NpgsqlDataSource.Create(b.ConnectionString);
}
DateTime Stamp(bool old) => new(2026, old ? 9 : 10, 1, 12, 0, 0, DateTimeKind.Utc);
NotificationEvent Graph(bool old, NotificationTemplate template) => new() {
    ScheduledAt = Stamp(old), Template = template,
    Queue = new DeliveryQueue { ScheduledAt = Stamp(old), ParentScheduledAt = Stamp(old), Template = template },
    Pushes = [new() { ParentScheduledAt = Stamp(old) }, new() { ParentScheduledAt = Stamp(old) }]
};
async Task<string> Snapshot(string schema)
{
    var tables = new[] { "notification_event", "delivery_queue", "push_delivery", "send_history", "notification_template" };
    var parts = new List<string>();
    foreach (var table in tables)
        parts.Add((string)(await Scalar(admin, $"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'[]') FROM {schema}.{table} t"))!);
    return string.Join("\n", parts);
}
async Task ExpectState(string state, Func<Task> action)
{
    try { await action(); }
    catch (Exception ex) when (SqlState(ex) == state) { return; }
    throw new InvalidOperationException($"Expected SQLSTATE {state}");
}
string? SqlState(Exception ex) => ex is PostgresException p ? p.SqlState : ex.InnerException is {} inner ? SqlState(inner) : null;
try
{
    engine = (string)(await Scalar(admin, "SELECT version()"))!;
    Require((long)(await Scalar(admin, "SELECT count(*) FROM pg_namespace WHERE nspname IN ('baseline','partitioned')"))! == 0,
        "refuse to modify a previously populated sample database");
    foreach (var partitioned in new[] { false, true })
    {
        var schema = partitioned ? "partitioned" : "baseline";
        await Sql(admin, Schema.Create(partitioned));
        if (partitioned)
        {
            // Bounded migration rehearsal: quiesce the source, copy keys and payloads, backfill
            // the parent's timestamp into children, and advance each destination identity sequence.
            await using var migration = await admin.BeginTransactionAsync();
            await Sql(admin, "LOCK TABLE baseline.notification_template,baseline.notification_event,baseline.delivery_queue,baseline.push_delivery,baseline.send_history IN ACCESS EXCLUSIVE MODE");
            await Sql(admin, Schema.CopyBaseline);
            foreach (var table in new[] { "notification_template", "notification_event", "delivery_queue", "push_delivery", "send_history" })
            {
                var projection = table switch {
                    "delivery_queue" => "to_jsonb(t)-'parent_scheduled_at'",
                    "push_delivery" => "to_jsonb(t)-'parent_scheduled_at'", _ => "to_jsonb(t)" };
                Require((bool)(await Scalar(admin, $"SELECT NOT EXISTS ((SELECT to_jsonb(t) FROM baseline.{table} t EXCEPT ALL SELECT {projection} FROM partitioned.{table} t) UNION ALL (SELECT {projection} FROM partitioned.{table} t EXCEPT ALL SELECT to_jsonb(t) FROM baseline.{table} t))"))!,
                    "migration preserves all original columns: " + table);
            }
            await migration.CommitAsync();
        }
        await using var source = Source();
        await using (var db = Context(partitioned, source))
        {
            if (!partitioned)
            {
                var template = new NotificationTemplate();
                db.AddRange(Graph(true, template), Graph(false, template));
                await db.SaveChangesAsync();
                var queues = await db.Queues.OrderBy(x => x.Id).ToListAsync();
                db.History.AddRange(queues.Select(q => new SendHistory { DeliveryQueueId = q.Id }));
                await db.SaveChangesAsync();
            }
            var graphs = await db.Events.AsNoTracking().Include(x => x.Queue).Include(x => x.Pushes).ToListAsync();
            Require(graphs.Count == 2 && graphs.All(x => x.Id > 0 && x.Queue?.NotificationEventId == x.Id &&
                x.Pushes.Count == 2 && x.Pushes.All(p => p.NotificationEventId == x.Id)), schema + ": EF loads complete graph with correct IDs and FKs");
        }
        if (partitioned)
        {
            await using var db = Context(true, source);
            await using var tx = await db.Database.BeginTransactionAsync();
            var graph = Graph(false, new NotificationTemplate());
            db.Events.Add(graph);
            await db.SaveChangesAsync();
            var history = new SendHistory { DeliveryQueueId = graph.Queue!.Id };
            db.History.Add(history);
            await db.SaveChangesAsync();
            Require(graph.Id > 2 && graph.Queue.Id > 2 && graph.Pushes.All(x => x.Id > 4) &&
                graph.Template.Id > 1 && history.Id > 2, "all migrated identity sequences generate IDs above copied rows");
            await tx.RollbackAsync();
        }
        var before = await Snapshot(schema);
        await using (var db = Context(partitioned, source))
        {
            await using var tx = await db.Database.BeginTransactionAsync();
            var oldStamp = Stamp(true);
            var q = partitioned
                ? await db.Queues.SingleAsync(x => x.Id == 1 && x.ParentScheduledAt == oldStamp)
                : await db.Queues.SingleAsync(x => x.Id == 1);
            q.ScheduledAt = Stamp(false).AddDays(3);
            await db.SaveChangesAsync();
            Require((partitioned
                ? await db.Queues.Where(x => x.Id == 1 && x.ParentScheduledAt == oldStamp).Select(x => x.ScheduledAt).SingleAsync()
                : await db.Queues.Where(x => x.Id == 1).Select(x => x.ScheduledAt).SingleAsync()) == q.ScheduledAt,
                schema + ": tracked queue rescheduling crosses midnight");
            if (partitioned)
                Require((string)(await Scalar((NpgsqlConnection)db.Database.GetDbConnection(),
                    "SELECT tableoid::regclass::text FROM partitioned.delivery_queue WHERE id=1 AND parent_scheduled_at='2026-09-01 12:00Z'"))! == "partitioned.delivery_queue_old",
                    "queue delivery change leaves parent-aligned partition unchanged");
            await tx.RollbackAsync();
        }
        await using (var db = Context(partitioned, source))
        {
            await using var tx = await db.Database.BeginTransactionAsync();
            await db.Events.Where(x => x.Id == 1).ExecuteDeleteAsync();
            Require(await db.Queues.CountAsync() == 1 && await db.Pushes.CountAsync() == 2 && await db.History.CountAsync() == 2,
                schema + ": parent delete cascades queue/push and leaves soft history");
            await tx.RollbackAsync();
        }
        await using (var db = Context(partitioned, source))
        {
            await using var tx = await db.Database.BeginTransactionAsync();
            await db.Templates.ExecuteDeleteAsync();
            Require(await db.Events.CountAsync() == 0 && await db.Queues.CountAsync() == 0 && await db.Pushes.CountAsync() == 0 && await db.History.CountAsync() == 2,
                schema + ": template delete cascades both parent and queue paths; history remains");
            await tx.RollbackAsync();
        }
        // Preserve the supplied database/ORM mismatch instead of letting EnsureCreated add uniqueness.
        await using (var tx = await admin.BeginTransactionAsync())
        {
            var extra = partitioned ? ",parent_scheduled_at" : "";
            await Sql(admin, $"INSERT INTO {schema}.delivery_queue(notification_event_id,scheduled_at,send_state,template_id{extra}) SELECT notification_event_id,scheduled_at,send_state,template_id{extra} FROM {schema}.delivery_queue WHERE id=1");
            Require((long)(await Scalar(admin, $"SELECT count(*) FROM {schema}.delivery_queue WHERE notification_event_id=1"))! == 2,
                schema + ": database permits duplicate queues despite ORM WithOne");
            await tx.RollbackAsync();
        }
        Require(await Snapshot(schema) == before, schema + ": rollback restores every fixture column");
    }
    await ExpectState("23514", () => Sql(admin, "UPDATE partitioned.notification_event SET scheduled_at=scheduled_at+interval '1 hour' WHERE id=1"));
    checks.Add("database rejects changing parent retention timestamp");
    await using (var tx = await admin.BeginTransactionAsync())
    {
        await Sql(admin, "INSERT INTO partitioned.notification_event(id,scheduled_at,content_body,external_ref_id,template_id) OVERRIDING SYSTEM VALUE SELECT id,'2026-10-01 13:00Z',content_body,external_ref_id,template_id FROM partitioned.notification_event WHERE id=1");
        Require((long)(await Scalar(admin, "SELECT count(*) FROM partitioned.notification_event WHERE id=1"))! == 2,
            "composite key permits duplicate bare IDs across partition timestamps");
        await tx.RollbackAsync();
    }

    foreach (var table in new[] { "delivery_queue", "push_delivery" })
    {
        await using var tx = await admin.BeginTransactionAsync();
        await Sql(admin, "INSERT INTO partitioned.notification_event(id,scheduled_at,content_body,external_ref_id,template_id) OVERRIDING SYSTEM VALUE SELECT id,'2026-10-01 12:00Z',content_body,external_ref_id,template_id FROM partitioned.notification_event WHERE id=1");
        // A valid alternate referenced key exists; the rejection must come from immutability, not a missing FK target.
        await ExpectState("23514", () => Sql(admin, $"UPDATE partitioned.{table} SET parent_scheduled_at='2026-10-01 12:00Z' WHERE id=1 AND parent_scheduled_at='2026-09-01 12:00Z'"));
        await tx.RollbackAsync();
        checks.Add(table + ": database rejects child retention timestamp reassignment even with valid alternate parent");
    }
    var beforeCollision = await Snapshot("partitioned");
    // Commit a legal duplicate bare queue ID to exercise fresh contexts, not just a rollback-only insertion.
    await Sql(admin, "INSERT INTO partitioned.delivery_queue(id,notification_event_id,parent_scheduled_at,scheduled_at,send_state,template_id) OVERRIDING SYSTEM VALUE SELECT 1,notification_event_id,parent_scheduled_at,scheduled_at,send_state,template_id FROM partitioned.delivery_queue WHERE id=2 AND parent_scheduled_at='2026-10-01 12:00Z'");
    var historyId = (long)(await Scalar(admin, "INSERT INTO partitioned.send_history(delivery_queue_id,detail) VALUES(1,'ambiguous live history') RETURNING id"))!;
    await using (var source = Source())
    await using (var db = Context(true, source))
    {
        await using var tx = await db.Database.BeginTransactionAsync();
        var oldStamp = Stamp(true);
        var liveStamp = Stamp(false);
        var q = await db.Queues.SingleAsync(x => x.Id == 1 && x.ParentScheduledAt == oldStamp);
        q.ScheduledAt = liveStamp.AddDays(3);
        await db.SaveChangesAsync();
        Require(await db.Queues.Where(x => x.Id == 1 && x.ParentScheduledAt == liveStamp).Select(x => x.ScheduledAt).SingleAsync() == liveStamp,
            "full-key EF reschedule leaves committed colliding live queue unchanged");
        await tx.RollbackAsync();
    }
    var collided = await Snapshot("partitioned");
    await ExpectState("23505", () => Sql(admin, Schema.HistoryIdentityGuard));
    Require(await Snapshot("partitioned") == collided, "archive guard rejects committed duplicate queue IDs without changing source or history");
    await Sql(admin, $"DELETE FROM partitioned.send_history WHERE id={historyId}; DELETE FROM partitioned.delivery_queue WHERE id=1 AND parent_scheduled_at='2026-10-01 12:00Z'");
    Require(await Snapshot("partitioned") == beforeCollision, "collision regression cleanup preserves complete original fixture");

    await using (var archiveAttempt = await admin.BeginTransactionAsync())
    {
        await Sql(admin, "LOCK TABLE partitioned.delivery_queue_old,partitioned.send_history IN SHARE MODE");
        await Sql(admin, Schema.HistoryIdentityGuard);
        await Sql(admin, "CREATE TABLE partitioned.guard_test AS TABLE partitioned.delivery_queue_old");
        await using (var writer = new NpgsqlConnection(connectionString))
        {
            await writer.OpenAsync();
            await Sql(writer, "INSERT INTO partitioned.delivery_queue(id,notification_event_id,parent_scheduled_at,scheduled_at,send_state,template_id) OVERRIDING SYSTEM VALUE SELECT 1,notification_event_id,parent_scheduled_at,scheduled_at,send_state,template_id FROM partitioned.delivery_queue WHERE id=2 AND parent_scheduled_at='2026-10-01 12:00Z'");
        }
        await Sql(admin, "LOCK TABLE partitioned.delivery_queue IN ACCESS EXCLUSIVE MODE");
        await ExpectState("23505", () => Sql(admin, Schema.HistoryIdentityGuard));
        await archiveAttempt.RollbackAsync();
    }
    Require((bool)(await Scalar(admin, "SELECT to_regclass('partitioned.guard_test') IS NULL"))!,
        "collision introduced after snapshot aborts archive transaction before retirement");
    await Sql(admin, "DELETE FROM partitioned.delivery_queue WHERE id=1 AND parent_scheduled_at='2026-10-01 12:00Z'");
    Require(await Snapshot("partitioned") == beforeCollision, "late-collision rejection preserves all original rows and soft history");

    // Each candidate gets its own pool so warming one query cannot accidentally warm another.
    var original = await Snapshot("partitioned");
    var candidates = new[] { "parent_id_update", "parent_qualified_update", "tracked_parent_update", "queue_reschedule", "parent_id_delete", "parent_qualified_delete", "template_delete", "expired_qualified_update" };
    foreach (var mode in new[] { "unprepared", "force_custom_plan", "force_generic_plan", "auto" })
    foreach (var warmth in new[] { "cold", "warm" })
    foreach (var candidate in candidates)
    {
        await using var source = Source(mode == "unprepared" ? "auto" : mode, mode != "unprepared");
        async Task<int> Execute(SampleContext db)
        {
            var id = candidate == "expired_qualified_update" ? 1L : 2L;
            var stamp = Stamp(id == 1);
            switch (candidate)
            {
                case "parent_id_update": return await db.Events.Where(x => x.Id == id).ExecuteUpdateAsync(s => s.SetProperty(x => x.ContentBody, "probe"));
                case "parent_qualified_update":
                case "expired_qualified_update": return await db.Events.Where(x => x.Id == id && x.ScheduledAt == stamp).ExecuteUpdateAsync(s => s.SetProperty(x => x.ContentBody, "probe"));
                case "tracked_parent_update":
                    var entity = new NotificationEvent { Id = id, ScheduledAt = stamp };
                    db.Attach(entity); entity.ContentBody = "probe";
                    db.Entry(entity).Property(x => x.ContentBody).IsModified = true;
                    return await db.SaveChangesAsync();
                case "queue_reschedule": return await db.Queues.Where(x => x.Id == id && x.ParentScheduledAt == stamp)
                    .ExecuteUpdateAsync(s => s.SetProperty(x => x.ScheduledAt, stamp.AddDays(2)));
                case "parent_id_delete": return await db.Events.Where(x => x.Id == id).ExecuteDeleteAsync();
                case "parent_qualified_delete": return await db.Events.Where(x => x.Id == id && x.ScheduledAt == stamp).ExecuteDeleteAsync();
                default: return await db.Templates.Where(x => x.Id == 1).ExecuteDeleteAsync();
            }
        }
        int warmPid = 0;
        var iterations = warmth == "warm" ? 8 : 1;
        // Cold controls use a separate pool; warm controls exercise the candidate's reused physical connection.
        await using (var controlSource = Source(mode == "unprepared" ? "auto" : mode, mode != "unprepared"))
        for (var i = 0; i < iterations; i++)
        {
            await using var db = Context(true, warmth == "warm" ? source : controlSource);
            await db.Database.OpenConnectionAsync();
            var pid = ((NpgsqlConnection)db.Database.GetDbConnection()).ProcessID;
            if (warmPid != 0 && warmth == "warm" && warmPid != pid) throw new Exception("Warm connection changed");
            warmPid = pid;
            await using var tx = await db.Database.BeginTransactionAsync();
            if (await Execute(db) != 1) throw new Exception("Unfenced candidate did not affect exactly one row");
            await tx.RollbackAsync();
        }
        await using var fence = await admin.BeginTransactionAsync();
        await Sql(admin, "LOCK TABLE partitioned.notification_event_old, partitioned.delivery_queue_old, partitioned.push_delivery_old IN SHARE MODE");
        await using var probe = Context(true, source);
        await probe.Database.OpenConnectionAsync();
        var conn = (NpgsqlConnection)probe.Database.GetDbConnection();
        if (warmth == "warm" && warmPid != conn.ProcessID) throw new Exception("Pool did not reuse warmed connection");
        var countersBefore = (string)(await Scalar(conn, "SELECT coalesce(jsonb_agg(jsonb_build_object('sql',statement,'custom',custom_plans,'generic',generic_plans))::text,'[]') FROM pg_prepared_statements"))!;
        if (warmth == "warm" && mode != "unprepared")
        {
            var preparedDml = JsonSerializer.Deserialize<JsonElement>(countersBefore).EnumerateArray()
                .Where(x => x.GetProperty("sql").GetString() is {} sql && (sql.StartsWith("UPDATE") || sql.StartsWith("DELETE"))).ToList();
            if (preparedDml.Count != 1 || preparedDml[0].GetProperty("custom").GetInt64() + preparedDml[0].GetProperty("generic").GetInt64() < 1)
                throw new Exception("Candidate DML was not automatically prepared and executed");
            if (preparedDml[0].GetProperty("sql").GetString()!.Contains("$1") &&
                (mode == "force_custom_plan" && preparedDml[0].GetProperty("generic").GetInt64() != 0 ||
                 mode == "force_generic_plan" && preparedDml[0].GetProperty("custom").GetInt64() != 0))
                throw new Exception("Prepared counters contradict forced plan mode");
        }
        string outcome;
        await using (var tx = await probe.Database.BeginTransactionAsync())
        {
            try
            {
                if (await Execute(probe) != 1) throw new Exception("Fenced candidate affected unexpected rows");
                outcome = "admitted_rolled_back";
            }
            catch (Exception ex) when (SqlState(ex) == "55P03") { outcome = "blocked_55P03"; }
            await tx.RollbackAsync();
        }
        var qualifiedLive = candidate is "parent_qualified_update" or "tracked_parent_update" or "queue_reschedule" or "parent_qualified_delete";
        // Forced generic mode also applies to unnamed extended-protocol execution before auto-prepare.
        var expected = qualifiedLive && mode != "force_generic_plan" ? "admitted_rolled_back" : "blocked_55P03";
        if (outcome != expected) throw new Exception($"{mode}/{warmth}/{candidate}: expected {expected}, got {outcome}");
        var countersAfter = (string)(await Scalar(conn, "SELECT coalesce(jsonb_agg(jsonb_build_object('sql',statement,'custom',custom_plans,'generic',generic_plans))::text,'[]') FROM pg_prepared_statements"))!;
        probes.Add(new { mode, warmth, candidate, outcome, warmExecutions = warmth == "warm" ? iterations : 0,
            reusedPhysicalConnection = warmth == "warm", before = JsonSerializer.Deserialize<JsonElement>(countersBefore), after = JsonSerializer.Deserialize<JsonElement>(countersAfter) });
        await fence.RollbackAsync();
    }
    Require(await Snapshot("partitioned") == original, "all 64 fenced EF probes and unfenced controls preserve complete fixture contents");

    // Independent committing writer and reader while the archive transaction holds expired-leaf locks.
    await using (var fence = await admin.BeginTransactionAsync())
    {
        await Sql(admin, "LOCK TABLE partitioned.notification_event_old,partitioned.delivery_queue_old,partitioned.push_delivery_old IN SHARE MODE");
        await Sql(admin, "LOCK TABLE partitioned.send_history IN SHARE MODE");
        await Sql(admin, Schema.HistoryIdentityGuard);
        await Sql(admin, "CREATE SCHEMA sample_archive");
        foreach (var table in new[] { "notification_event", "delivery_queue", "push_delivery" })
            await Sql(admin, $"CREATE TABLE sample_archive.{table} AS TABLE partitioned.{table}_old");
        await Sql(admin, "CREATE TABLE sample_archive.send_history AS SELECT h.* FROM partitioned.send_history h JOIN partitioned.delivery_queue_old q ON q.id=h.delivery_queue_id");
        await using var writerSource = Source();
        await using var readerSource = Source();
        var acknowledgements = new List<(long Id, DateTime Stamp, string Ref)>();
        for (var i = 0; i < 10; i++)
        {
            await using (var db = Context(true, writerSource))
            {
                var template = await db.Templates.SingleAsync();
                var graph = Graph(false, template);
                db.Events.Add(graph);
                await db.SaveChangesAsync();
                acknowledgements.Add((graph.Id, graph.ScheduledAt, graph.ExternalRefId));
            }
            await using var reader = Context(true, readerSource);
            var ack = acknowledgements[^1];
            Require(await reader.Events.AnyAsync(x => x.Id == ack.Id && x.ScheduledAt == ack.Stamp && x.ExternalRefId == ack.Ref),
                $"independent reader observes committed live graph {i + 1} while expired fence held");
        }
        foreach (var table in new[] { "notification_event", "delivery_queue", "push_delivery" })
            Require((bool)(await Scalar(admin, $"SELECT NOT EXISTS ((TABLE sample_archive.{table} EXCEPT ALL TABLE partitioned.{table}_old) UNION ALL (TABLE partitioned.{table}_old EXCEPT ALL TABLE sample_archive.{table}))"))!,
                "local archive snapshot still matches expired " + table + " after live commits");
        Require((bool)(await Scalar(admin, "SELECT NOT EXISTS ((SELECT h.* FROM partitioned.send_history h JOIN partitioned.delivery_queue_old q ON q.id=h.delivery_queue_id EXCEPT ALL TABLE sample_archive.send_history) UNION ALL (TABLE sample_archive.send_history EXCEPT ALL SELECT h.* FROM partitioned.send_history h JOIN partitioned.delivery_queue_old q ON q.id=h.delivery_queue_id))"))!,
            "local archive snapshot preserves expired soft history");
        // Probe connections and transactions have been released before this parent lock upgrade.
        await Sql(admin, "SET LOCAL lock_timeout='2s'; LOCK TABLE partitioned.notification_event,partitioned.delivery_queue,partitioned.push_delivery IN ACCESS EXCLUSIVE MODE");
        // Recheck under parent locks: a live writer could introduce a collision after the initial snapshot.
        await Sql(admin, Schema.HistoryIdentityGuard);
        await Sql(admin, """
            DELETE FROM partitioned.send_history h USING partitioned.delivery_queue_old q WHERE h.delivery_queue_id=q.id;
            DROP TABLE partitioned.push_delivery_old;
            DROP TABLE partitioned.delivery_queue_old;
            ALTER TABLE partitioned.notification_event DETACH PARTITION partitioned.notification_event_old;
            DROP TABLE partitioned.notification_event_old;
            """);
        await fence.CommitAsync();
        await using var verify = Context(true, readerSource);
        Require(await verify.Events.CountAsync() == 11 && await verify.Queues.CountAsync() == 11 &&
            await verify.Pushes.CountAsync() == 22 && await verify.History.CountAsync() == 1,
            "fresh EF reads after committed retirement preserve all live rows and clean expired soft history");
        foreach (var ack in acknowledgements)
        {
            var e = await verify.Events.AsNoTracking().Include(x => x.Queue).Include(x => x.Pushes)
                .SingleAsync(x => x.Id == ack.Id && x.ScheduledAt == ack.Stamp);
            if (e.ExternalRefId != ack.Ref || e.ContentBody != "synthetic <p>payload</p>" || e.Queue?.SendState != "pending" || e.Pushes.Count != 2)
                throw new Exception("Acknowledged graph lost or changed");
        }
        checks.Add("all ten acknowledged graphs survive committed retirement with exact body/reference/state and children");
    }
    passed = true;
}
finally
{
    var receipt = new {
        passed, engine, framework = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
        efVersion = typeof(DbContext).Assembly.GetName().Version?.ToString(),
        npgsqlVersion = typeof(NpgsqlConnection).Assembly.GetName().Version?.ToString(),
        providerVersion = typeof(NpgsqlDbContextOptionsBuilderExtensions).Assembly.GetName().Version?.ToString(),
        checks, probes, sql = capture.Statements.Order().ToArray()
    };
    await File.WriteAllTextAsync(output, JsonSerializer.Serialize(receipt, new JsonSerializerOptions { WriteIndented = true }) + "\n");
    Console.WriteLine($"Passed={passed}; checks={checks.Count}; probes={probes.Count}; receipt={output}");
}
