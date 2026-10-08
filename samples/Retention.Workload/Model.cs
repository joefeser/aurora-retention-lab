using Microsoft.EntityFrameworkCore;
using System.Text.RegularExpressions;

namespace Retention.Workload;

public sealed class Event
{
    public long Id { get; set; }
    public string RecipientId { get; set; } = "";
    public string? MessageText { get; set; }
    public string SubjectText { get; set; } = "";
    public DateTime ScheduledAt { get; set; }
    public DateTime? ExpiresAt { get; set; }
    public int NotificationTemplateId { get; set; }
    public DateTime CreatedAt { get; set; }
    public Guid ExternalRefId { get; set; }
    public short SendState { get; set; }
    public string? ContentBody { get; set; }
    public string? PushNotificationText { get; set; }
    public string? SmsNotificationText { get; set; }
    public string? EmailNotificationText { get; set; }
    public DateTime? TargetTime { get; set; }
    public string? Metadata { get; set; }
    public int RemainingMinutes { get; set; }
    public Queue? Queue { get; set; }
    public Template Template { get; set; } = null!;
    public List<Push> Pushes { get; set; } = [];
}
public sealed class Queue
{
    public long Id { get; set; }
    public string RecipientId { get; set; } = "";
    public int NotificationTemplateId { get; set; }
    public long NotificationEventId { get; set; }
    public string? MessageText { get; set; }
    public string SubjectText { get; set; } = "";
    public string SendDetails { get; set; } = "{}";
    public bool AllTargetsAttempted { get; set; }
    public DateTime? ExpiresAt { get; set; }
    public DateTime ScheduledAt { get; set; }
    public DateTime CreatedAt { get; set; }
    public short SendState { get; set; }
    public string? ContentBody { get; set; }
    public string? PushNotificationText { get; set; }
    public string? SmsNotificationText { get; set; }
    public string? EmailNotificationText { get; set; }
    public string? Metadata { get; set; }
    public DateTime ParentScheduledAt { get; set; }
    public Event Event { get; set; } = null!;
    public Template Template { get; set; } = null!;
}
public sealed class Push
{
    public long Id { get; set; }
    public long NotificationEventId { get; set; }
    public DateTime ParentScheduledAt { get; set; }
    public string Body { get; set; } = "";
}
public sealed class Template
{
    public int Id { get; set; }
    public string Name { get; set; } = "";
}
public abstract class WorkContext(DbContextOptions options) : DbContext(options)
{
    protected abstract bool Partitioned { get; }
    public DbSet<Event> Events => Set<Event>();
    public DbSet<Queue> Queues => Set<Queue>();
    protected override void OnModelCreating(ModelBuilder b)
    {
        b.HasDefaultSchema(Partitioned ? "bench_partition" : "bench_baseline");
        b.Entity<Event>().ToTable("notification_event");
        b.Entity<Queue>().ToTable("delivery_queue");
        b.Entity<Push>().ToTable("push_delivery");
        b.Entity<Template>().ToTable("notification_template");
        b.Entity<Event>().HasOne(x=>x.Template).WithMany().HasForeignKey(x=>x.NotificationTemplateId);
        b.Entity<Queue>().HasOne(x=>x.Template).WithMany().HasForeignKey(x=>x.NotificationTemplateId);
        if (Partitioned)
        {
            b.Entity<Event>().HasKey(x=>new{x.Id,x.ScheduledAt});
            b.Entity<Queue>().HasKey(x=>new{x.Id,x.ParentScheduledAt});
            b.Entity<Push>().HasKey(x=>new{x.Id,x.ParentScheduledAt});
            b.Entity<Queue>().HasOne(x=>x.Event).WithOne(x=>x.Queue).HasForeignKey<Queue>(x=>new{x.NotificationEventId,x.ParentScheduledAt});
            b.Entity<Event>().HasMany(x=>x.Pushes).WithOne().HasForeignKey(x=>new{x.NotificationEventId,x.ParentScheduledAt});
        }
        else
        {
            b.Entity<Queue>().Ignore(x=>x.ParentScheduledAt);
            b.Entity<Push>().Ignore(x=>x.ParentScheduledAt);
            b.Entity<Queue>().HasOne(x=>x.Event).WithOne(x=>x.Queue).HasForeignKey<Queue>(x=>x.NotificationEventId);
            b.Entity<Event>().HasMany(x=>x.Pushes).WithOne().HasForeignKey(x=>x.NotificationEventId);
        }
        b.Entity<Event>().Property(x=>x.Metadata).HasColumnType("jsonb");
        b.Entity<Queue>().Property(x=>x.Metadata).HasColumnType("jsonb");
        b.Entity<Queue>().Property(x=>x.SendDetails).HasColumnType("jsonb");
        b.Entity<Event>().Property(x=>x.ContentBody).HasMaxLength(10485760);
        b.Entity<Queue>().Property(x=>x.ContentBody).HasMaxLength(10485760);
        foreach(var entity in b.Model.GetEntityTypes())
            foreach(var property in entity.GetProperties())
                property.SetColumnName(Regex.Replace(property.Name,"(?<!^)([A-Z])","_$1").ToLowerInvariant());
    }
}
public sealed class Baseline(DbContextOptions options) : WorkContext(options) {protected override bool Partitioned=>false;}
public sealed class PartitionContext(DbContextOptions options) : WorkContext(options) {protected override bool Partitioned=>true;}
