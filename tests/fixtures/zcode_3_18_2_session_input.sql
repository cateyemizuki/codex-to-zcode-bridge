-- Verbatim relevant native SQL from Desktop 3.18.2 resources/glm/zcode.cjs.
-- Source SHA-256: 08726b5ef69e1e9072828baa4656713cb3aac93bb096938681278c5600312b2c
-- Base session DDL, final input-ledger DDL (migration 0018), and F0n insert.

create table if not exists session (
        id text primary key,
        project_id text not null,
        workspace_id text,
        parent_id text,
        slug text not null,
        directory text not null,
        path text,
        title text not null,
        version text not null,
        share_url text,
        summary_additions integer,
        summary_deletions integer,
        summary_files integer,
        summary_diffs text,
        revert text,
        permission text,
        time_created integer not null,
        time_updated integer not null,
        time_compacting integer,
        time_archived integer
      );

create table session_input (
        id text primary key,
        session_id text not null references session(id) on delete cascade,
        kind text not null,
        delivery text not null check(delivery in ('startNow', 'guide', 'queue')),
        payload text not null,
        admitted_sequence integer not null,
        promoted_sequence integer,
        promoted_message_id text,
        status text not null check(status in ('admitted', 'promoted', 'cancelled', 'discarded', 'failed')),
        status_reason text,
        time_created integer not null,
        time_updated integer not null
      );

-- NATIVE_F0N_INSERT
insert into session_input (
        id, session_id, kind, delivery, payload,
        admitted_sequence, status, time_created, time_updated
      )
      values (
        ?, ?, ?, ?, ?,
        (
          select coalesce(max(admitted_sequence), -1) + 1
          from session_input
          where session_id = ?
        ),
        'admitted', ?, ?
      )
      on conflict(id) do update set
        kind = excluded.kind,
        delivery = excluded.delivery,
        payload = excluded.payload,
        time_updated = excluded.time_updated
