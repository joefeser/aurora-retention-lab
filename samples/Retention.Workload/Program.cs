using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Running;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Exporters;
using Microsoft.EntityFrameworkCore;
using Npgsql;
using Retention.Workload;

BenchmarkSwitcher.FromAssembly(typeof(Program).Assembly).Run(args);

[MemoryDiagnoser]
[SimpleJob(RuntimeMoniker.Net10_0, launchCount:1, warmupCount:3, iterationCount:10, invocationCount:1)]
[JsonExporterAttribute.Full]
public class WorkloadBenchmarks
{
    [Params(false,true)] public bool Partitioned {get;set;}
    [Params(false,true)] public bool AutoPrepare {get;set;}
    private NpgsqlDataSource source=null!;
    private List<(long Id,DateTime Timestamp,Guid Reference)> rows=[];
    private DbContextOptions options=null!;
    private WorkContext Context()=>Partitioned?new PartitionContext(options):new Baseline(options);
    [GlobalSetup]
    public async Task Setup()
    {
        var cs=new NpgsqlConnectionStringBuilder(Environment.GetEnvironmentVariable("RETENTION_BENCH_CONNECTION")??throw new Exception("Use the local benchmark wrapper"));
        if(cs.Host!="127.0.0.1"||cs.Database!="retention_perf")throw new Exception("Only disposable local retention_perf is allowed");
        cs.MaxPoolSize=1;cs.MaxAutoPrepare=AutoPrepare?50:0;cs.AutoPrepareMinUsages=2;
        source=NpgsqlDataSource.Create(cs.ConnectionString);
        options=new DbContextOptionsBuilder().UseNpgsql(source).Options;
        await using var db=Context();
        // Exactly 100 supplied-shape rows, including tail payloads; selection does not vary across cases.
        var selected=await db.Events.AsNoTracking().Where(x=>x.Id<=100).OrderBy(x=>x.Id)
            .Select(x=>new{x.Id,x.ScheduledAt,x.ExternalRefId}).ToListAsync();
        rows=selected.Select(x=>(x.Id,x.ScheduledAt,x.ExternalRefId)).ToList();
        if(rows.Count!=100)throw new Exception("Expected exactly 100 benchmark identities");
    }
    [GlobalCleanup] public void Cleanup()=>source.Dispose();
    [Benchmark(OperationsPerInvoke=100)]
    public async Task<long> ReadParentById()
    {
        long bytes=0;
        foreach(var row in rows)
        {
            await using var db=Context();
            var entity=await db.Events.AsNoTracking().SingleAsync(x=>x.Id==row.Id);
            bytes+=entity.ContentBody?.Length??0;
        }
        return bytes;
    }
    [Benchmark(OperationsPerInvoke=100)]
    public async Task<long> ReadParentQualified()
    {
        long bytes=0;
        foreach(var row in rows)
        {
            await using var db=Context();
            var entity=await db.Events.AsNoTracking().SingleAsync(x=>x.Id==row.Id&&x.ScheduledAt==row.Timestamp);
            bytes+=entity.ContentBody?.Length??0;
        }
        return bytes;
    }
    [Benchmark(OperationsPerInvoke=100)]
    public async Task<long> ReadParentByExternalReference()
    {
        long bytes=0;
        foreach(var row in rows)
        {
            await using var db=Context();
            var entity=await db.Events.AsNoTracking().SingleAsync(x=>x.ExternalRefId==row.Reference);
            bytes+=entity.ContentBody?.Length??0;
        }
        return bytes;
    }
    private async Task<long> ReadGraph(bool split)
    {
        long bytes=0;
        foreach(var row in rows)
        {
            await using var db=Context();
            IQueryable<Event> query=db.Events.AsNoTracking().Include(x=>x.Template)
                .Include(x=>x.Queue).ThenInclude(x=>x!.Template).Include(x=>x.Pushes);
            query=split?query.AsSplitQuery():query.AsSingleQuery();
            var entity=await query.SingleAsync(x=>x.Id==row.Id&&x.ScheduledAt==row.Timestamp);
            if(entity.Queue is null||entity.Pushes.Count!=3)throw new Exception("Incomplete source-shaped relationship graph");
            bytes+=(entity.ContentBody?.Length??0)+(entity.Queue.ContentBody?.Length??0);
        }
        return bytes;
    }
    [Benchmark(OperationsPerInvoke=100)] public Task<long> ReadGraphSingleQuery()=>ReadGraph(false);
    [Benchmark(OperationsPerInvoke=100)] public Task<long> ReadGraphSplitQuery()=>ReadGraph(true);
    [Benchmark(OperationsPerInvoke=100)]
    public async Task RescheduleTrackedQueueRollback()
    {
        foreach(var row in rows)
        {
            await using var db=Context();
            await using var tx=await db.Database.BeginTransactionAsync();
            var queue=Partitioned
                ?await db.Queues.SingleAsync(x=>x.Id==row.Id&&x.ParentScheduledAt==row.Timestamp)
                :await db.Queues.SingleAsync(x=>x.Id==row.Id);
            queue.ScheduledAt=queue.ScheduledAt.AddHours(12);
            await db.SaveChangesAsync();
            await tx.RollbackAsync();
        }
    }
}
