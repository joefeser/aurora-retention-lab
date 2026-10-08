using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Diagnostics;
using System.Data.Common;
using System.Text.RegularExpressions;

public sealed class NotificationEvent
{
    public long Id { get; set; }
    public DateTime ScheduledAt { get; set; }
    public string ContentBody { get; set; } = "synthetic <p>payload</p>";
    public string ExternalRefId { get; set; } = Guid.NewGuid().ToString();
    public long TemplateId { get; set; }
    public NotificationTemplate Template { get; set; } = null!;
    public DeliveryQueue? Queue { get; set; }
    public List<PushDelivery> Pushes { get; set; } = [];
}
public sealed class DeliveryQueue
{
    public long Id { get; set; }
    public long NotificationEventId { get; set; }
    public DateTime ParentScheduledAt { get; set; }
    public DateTime ScheduledAt { get; set; }
    public string SendState { get; set; } = "pending";
    public long TemplateId { get; set; }
    public NotificationTemplate Template { get; set; } = null!;
    public NotificationEvent Event { get; set; } = null!;
}
public sealed class PushDelivery
{
    public long Id { get; set; }
    public long NotificationEventId { get; set; }
    public DateTime ParentScheduledAt { get; set; }
    public NotificationEvent Event { get; set; } = null!;
}
public sealed class SendHistory
{
    public long Id { get; set; }
    public long DeliveryQueueId { get; set; }
    public string Detail { get; set; } = "synthetic history";
}
public sealed class NotificationTemplate
{
    public long Id { get; set; }
    public string Name { get; set; } = "synthetic template";
}
public abstract class SampleContext(DbContextOptions options) : DbContext(options)
{
    public abstract bool Partitioned { get; }
    public DbSet<NotificationEvent> Events => Set<NotificationEvent>();
    public DbSet<DeliveryQueue> Queues => Set<DeliveryQueue>();
    public DbSet<PushDelivery> Pushes => Set<PushDelivery>();
    public DbSet<SendHistory> History => Set<SendHistory>();
    public DbSet<NotificationTemplate> Templates => Set<NotificationTemplate>();
    protected override void OnModelCreating(ModelBuilder m)
    {
        m.HasDefaultSchema(Partitioned ? "partitioned" : "baseline");
        m.Entity<NotificationEvent>().ToTable("notification_event");
        m.Entity<DeliveryQueue>().ToTable("delivery_queue");
        m.Entity<PushDelivery>().ToTable("push_delivery");
        m.Entity<SendHistory>().ToTable("send_history");
        m.Entity<NotificationTemplate>().ToTable("notification_template");
        if (Partitioned)
        {
            m.Entity<NotificationEvent>().HasKey(x => new { x.Id, x.ScheduledAt });
            m.Entity<DeliveryQueue>().HasKey(x => new { x.Id, x.ParentScheduledAt });
            m.Entity<PushDelivery>().HasKey(x => new { x.Id, x.ParentScheduledAt });
            m.Entity<DeliveryQueue>().HasOne(x => x.Event).WithOne(x => x.Queue)
                .HasForeignKey<DeliveryQueue>(x => new { x.NotificationEventId, x.ParentScheduledAt });
            m.Entity<PushDelivery>().HasOne(x => x.Event).WithMany(x => x.Pushes)
                .HasForeignKey(x => new { x.NotificationEventId, x.ParentScheduledAt });
        }
        else
        {
            m.Entity<DeliveryQueue>().Ignore(x => x.ParentScheduledAt);
            m.Entity<PushDelivery>().Ignore(x => x.ParentScheduledAt);
            m.Entity<DeliveryQueue>().HasOne(x => x.Event).WithOne(x => x.Queue)
                .HasForeignKey<DeliveryQueue>(x => x.NotificationEventId);
            m.Entity<PushDelivery>().HasOne(x => x.Event).WithMany(x => x.Pushes)
                .HasForeignKey(x => x.NotificationEventId);
        }
        m.Entity<NotificationEvent>().HasOne(x => x.Template).WithMany().HasForeignKey(x => x.TemplateId);
        m.Entity<DeliveryQueue>().HasOne(x => x.Template).WithMany().HasForeignKey(x => x.TemplateId);
        // Intentionally no history navigation/FK: this reproduces the supplied soft relationship.
        foreach (var entity in m.Model.GetEntityTypes())
        {
            m.Entity(entity.ClrType).Property<long>("Id").UseIdentityAlwaysColumn();
            foreach (var property in entity.GetProperties())
                property.SetColumnName(Regex.Replace(property.Name, "(?<!^)([A-Z])", "_$1").ToLowerInvariant());
            foreach (var fk in entity.GetForeignKeys()) fk.DeleteBehavior = DeleteBehavior.Cascade;
        }
    }
}
public sealed class BaselineContext(DbContextOptions options) : SampleContext(options)
{ public override bool Partitioned => false; }
public sealed class PartitionContext(DbContextOptions options) : SampleContext(options)
{ public override bool Partitioned => true; }
public sealed class SqlCapture : DbCommandInterceptor
{
    public HashSet<string> Statements { get; } = [];
    public override ValueTask<InterceptionResult<DbDataReader>> ReaderExecutingAsync(DbCommand c, CommandEventData e,
        InterceptionResult<DbDataReader> r, CancellationToken token = default)
    { Statements.Add(c.CommandText); return ValueTask.FromResult(r); }
    public override ValueTask<InterceptionResult<int>> NonQueryExecutingAsync(DbCommand c, CommandEventData e,
        InterceptionResult<int> r, CancellationToken token = default)
    { Statements.Add(c.CommandText); return ValueTask.FromResult(r); }
}
