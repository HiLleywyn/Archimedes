-- ──────────────────────────────────────────────────────────────────────────
-- Archimedes -- database schema.
--
-- Idempotent: every statement is CREATE ... IF NOT EXISTS, so this file is
-- safe to run on every boot. Adding a column later means adding an
-- ALTER ... IF NOT EXISTS block at the bottom.
-- ──────────────────────────────────────────────────────────────────────────

-- ── Per-guild settings ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id              BIGINT PRIMARY KEY,
    ai_chat_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    ai_commentary_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ai_flavor_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    ai_events_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    ai_ambient_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    ai_threaded           BOOLEAN NOT NULL DEFAULT TRUE,
    ai_persona_name       TEXT,
    ai_promptchat         TEXT,
    ai_promptcommentary   TEXT,
    ai_promptevents       TEXT,
    ai_promptflavor       TEXT,
    ai_reply_delete_after INTEGER NOT NULL DEFAULT 0,
    ai_cmd_delete_after   INTEGER NOT NULL DEFAULT 0,
    search_backend        TEXT,
    tools_backend         TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Conversation history ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_conversations (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    guild_id    BIGINT NOT NULL,
    history_key TEXT   NOT NULL DEFAULT 'default',
    role        TEXT   NOT NULL,
    content     TEXT   NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_conv_lookup
    ON ai_conversations (user_id, guild_id, history_key, id);

-- ── Per-user rolling memory summary ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_user_memory (
    user_id           BIGINT NOT NULL,
    guild_id          BIGINT NOT NULL,
    memory            TEXT   NOT NULL DEFAULT '',
    message_count     INTEGER NOT NULL DEFAULT 0,
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

-- ── Trait engine (time-decayed behaviour signals) ──────────────────────────
CREATE TABLE IF NOT EXISTS ai_user_traits (
    user_id          BIGINT NOT NULL,
    guild_id         BIGINT NOT NULL,
    trait            TEXT   NOT NULL,
    weight           DOUBLE PRECISION NOT NULL DEFAULT 0,
    sample_size      INTEGER NOT NULL DEFAULT 0,
    last_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, trait)
);

CREATE TABLE IF NOT EXISTS ai_user_events (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    guild_id   BIGINT NOT NULL,
    event_type TEXT   NOT NULL,
    subtype    TEXT   NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_events_lookup
    ON ai_user_events (user_id, guild_id, created_at);

CREATE TABLE IF NOT EXISTS ai_reaction_memory (
    user_id  BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    category TEXT   NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, category)
);

CREATE TABLE IF NOT EXISTS ai_tool_memory (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    tool_key     TEXT   NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, tool_key)
);

CREATE TABLE IF NOT EXISTS ai_opt_outs (
    user_id    BIGINT NOT NULL,
    guild_id   BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

-- ── Per-guild model picker ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_model_defaults (
    guild_id   BIGINT NOT NULL,
    category   TEXT   NOT NULL,
    provider   TEXT   NOT NULL,
    model      TEXT   NOT NULL,
    updated_by BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, category)
);

-- ── Per-channel ambient context feed ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_context (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    kind       TEXT   NOT NULL DEFAULT 'message',
    summary    TEXT   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_channel_context_lookup
    ON channel_context (guild_id, channel_id, created_at);

-- ── Memory sidecar: long-term facts + episodes ─────────────────────────────
CREATE TABLE IF NOT EXISTS archimedes_facts (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL DEFAULT 'auto',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS archimedes_episodes (
    id         BIGSERIAL PRIMARY KEY,
    scope      TEXT   NOT NULL,
    summary    TEXT   NOT NULL,
    tags       TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_archimedes_episodes_scope
    ON archimedes_episodes (scope, created_at);

CREATE TABLE IF NOT EXISTS archimedes_passive_channels (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    enabled_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);

-- ── Training corpus: every chat turn, append-only ──────────────────────────
CREATE TABLE IF NOT EXISTS archimedes_training_turns (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    guild_id        BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    user_message    TEXT   NOT NULL,
    assistant_reply TEXT   NOT NULL,
    messages        JSONB  NOT NULL DEFAULT '[]',
    model           TEXT   NOT NULL DEFAULT '',
    feedback        SMALLINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Saved Archimedes answers (.arch save) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS archimedes_saved_messages (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT NOT NULL,
    guild_id           BIGINT NOT NULL,
    channel_id         BIGINT NOT NULL,
    archimedes_message_id   BIGINT NOT NULL,
    trigger_message_id BIGINT,
    prompt_text        TEXT   NOT NULL DEFAULT '',
    response_text      TEXT   NOT NULL DEFAULT '',
    jump_url           TEXT,
    saved_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, guild_id, archimedes_message_id)
);

-- ── Per-member reply mode (.arch chat | threads) ──────────────────────────
CREATE TABLE IF NOT EXISTS archimedes_reply_modes (
    user_id  BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    mode     TEXT   NOT NULL DEFAULT 'thread',
    PRIMARY KEY (user_id, guild_id)
);

-- ── Custom emoji meaning index ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guild_emoji_meanings (
    guild_id    BIGINT NOT NULL,
    emoji_id    BIGINT NOT NULL,
    name        TEXT   NOT NULL,
    description TEXT   NOT NULL DEFAULT '',
    animated    BOOLEAN NOT NULL DEFAULT FALSE,
    category    TEXT,
    source      TEXT   NOT NULL DEFAULT 'vision',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, emoji_id)
);

CREATE TABLE IF NOT EXISTS guild_emoji_usage (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    emoji_id   BIGINT NOT NULL,
    channel_id BIGINT,
    context    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Staff audit feed ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff_audit (
    id         BIGSERIAL PRIMARY KEY,
    scope      TEXT   NOT NULL,
    guild_id   BIGINT NOT NULL,
    actor_id   BIGINT NOT NULL,
    action     TEXT   NOT NULL,
    severity   TEXT   NOT NULL DEFAULT 'info',
    details    TEXT   NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_staff_audit_lookup
    ON staff_audit (guild_id, scope, created_at);

-- ── Lua plugins: the installed-plugin registry ─────────────────────────────
-- Every plugin Archimedes knows about has a row here -- bundled plugins that
-- ship in plugins/ and plugins pulled from the marketplace alike. The Lua
-- source of a marketplace plugin is stored in this row so an installed,
-- enabled plugin survives a container redeploy with no disk state.
CREATE TABLE IF NOT EXISTS installed_plugins (
    plugin_id    TEXT PRIMARY KEY,
    name         TEXT    NOT NULL,
    version      TEXT    NOT NULL DEFAULT '0.0.0',
    origin       TEXT    NOT NULL DEFAULT 'marketplace',
    description  TEXT    NOT NULL DEFAULT '',
    author       TEXT    NOT NULL DEFAULT '',
    category     TEXT    NOT NULL DEFAULT 'General',
    source       TEXT    NOT NULL DEFAULT '',
    source_repo  TEXT    NOT NULL DEFAULT '',
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    installed_by BIGINT,
    installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Lua plugins: the generic per-plugin document store ─────────────────────
-- A namespaced document store every plugin can use without writing SQL. Each
-- record is a JSON document with an auto-assigned id. Plugins that belong to
-- a suite can share a namespace -- the productivity plugins (notes, tasks,
-- events, groups) all read and write the 'productivity' namespace.
CREATE TABLE IF NOT EXISTS plugin_storage (
    id         BIGSERIAL PRIMARY KEY,
    namespace  TEXT  NOT NULL,
    collection TEXT  NOT NULL,
    doc        JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plugin_storage_lookup
    ON plugin_storage (namespace, collection, id);
CREATE INDEX IF NOT EXISTS idx_plugin_storage_doc
    ON plugin_storage USING GIN (doc);

-- ── Lua plugins: the namespaced key/value store ────────────────────────────
-- A simple upsert-by-key store, separate from the document store above. The
-- document store has no unique key, so a key/value `set` belongs here where
-- the (namespace, key) primary key gives atomic upsert and delete. Namespace
-- is manifest.storage, so a plugin suite shares its key/value space too.
CREATE TABLE IF NOT EXISTS plugin_kv (
    namespace  TEXT  NOT NULL,
    key        TEXT  NOT NULL,
    value      JSONB NOT NULL DEFAULT 'null',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (namespace, key)
);

-- ── Removed: the productivity_* tables ─────────────────────────────────────
-- Notes, tasks, events and groups are no longer a built-in cog. They ship as
-- Lua plugins backed by plugin_storage, so the old relational tables are gone.
DROP TABLE IF EXISTS productivity_item_shares;
DROP TABLE IF EXISTS productivity_items;
DROP TABLE IF EXISTS productivity_group_invites;
DROP TABLE IF EXISTS productivity_group_members;
DROP TABLE IF EXISTS productivity_groups;
