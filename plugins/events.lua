-- events.lua -- the Events plugin for Archimedes.
--
-- Calendar events backed by the shared `productivity` document store. Every
-- event has a scheduled time; tasks and events can carry a reminder, and a
-- one-minute loop DMs the owner (or every group member) when one falls due.
--
-- Standalone plugin: registers the `.event` command group and a reminder
-- loop. It shares the `productivity` namespace with notes, tasks and groups.

local PAGE = 10

local M = {}

M.manifest = {
  id = "events",
  name = "Events",
  version = "1.0.0",
  description = "Calendar events with reminders, sharing and groups.",
  author = "HiLleywyn",
  category = "Productivity",
  storage = "productivity",
}

local TIME_HINT = "Could not read that time. Try `in 2h`, `in 3d`, or "
  .. "`2026-06-01 14:30`."

-- ── helpers ──────────────────────────────────────────────────────────────────
local function is_member(grp, user_id)
  for _, uid in ipairs(grp.members or {}) do
    if uid == user_id then return true end
  end
  return false
end

local function scope_for(ctx, group_id)
  if not group_id then
    return "user", ctx.author_id
  end
  local grp = ctx.store.get("groups", group_id)
  if not grp then
    return nil, nil, "There is no group #" .. group_id .. "."
  end
  if not is_member(grp, ctx.author_id) then
    return nil, nil, "You are not a member of group #" .. group_id .. "."
  end
  return "group", group_id
end

local function require_event(ctx, item_id, need_edit)
  if not item_id then
    return nil, false, "Give the event id."
  end
  local item = ctx.store.get("items", item_id)
  if not item or item.kind ~= "event" then
    return nil, false, "There is no event #" .. tostring(item_id) .. "."
  end
  if item.owner_kind == "group" then
    local grp = ctx.store.get("groups", item.owner_id)
    if not grp or not is_member(grp, ctx.author_id) then
      return nil, false, "That event belongs to a group you are not in."
    end
    return item, true
  end
  if item.owner_id == ctx.author_id then
    return item, true
  end
  for _, share in ipairs(item.shares or {}) do
    if share.user == ctx.author_id then
      if need_edit and not share.can_edit then
        return nil, false, "That event is shared with you as view-only."
      end
      return item, share.can_edit and true or false
    end
  end
  return nil, false, "You do not have access to that event."
end

local function split_title(text)
  local title = text:match("^([^\n]*)") or ""
  local body = text:match("^[^\n]*\n(.*)$") or ""
  return title:sub(1, 300), body
end

local function first_mention(ctx)
  for _, m in ipairs(ctx.mentions) do
    if not m.bot then return m end
  end
  return nil
end

local function dest_label(ctx, kind, id)
  if kind == "group" then return "group #" .. id end
  if id == ctx.author_id then return "your personal space" end
  return ctx.user_name(id)
end

local function parse_dest(ctx, rest)
  local first = (rest or ""):match("^%s*(%S+)")
  if first then
    local low = first:lower()
    if low == "me" or low == "self" or low == "mine" then
      return "user", ctx.author_id
    end
    local gid = first:match("^#(%d+)$")
    if gid then
      local kind, oid, err = scope_for(ctx, gid)
      if err then return nil, nil, err end
      return kind, oid
    end
  end
  local mention = first_mention(ctx)
  if mention then return "user", mention.id end
  return nil, nil, "Destination must be `me`, an @mention, or `#<groupid>`."
end

local function by_time(a, b)
  return (a.due_at or 0) < (b.due_at or 0)
end

local function list_pages(title, items, empty)
  if #items == 0 then
    return { { title = title, description = empty, color = arch.colors.neutral } }
  end
  table.sort(items, by_time)
  local pages = {}
  for start = 1, #items, PAGE do
    local lines = {}
    for i = start, math.min(start + PAGE - 1, #items) do
      local it = items[i]
      lines[#lines + 1] = "`#" .. it.id .. "` " .. arch.clip(it.title, 70)
        .. " -- " .. arch.fmt_time(it.due_at)
    end
    pages[#pages + 1] = {
      title = title, color = arch.colors.gold,
      description = table.concat(lines, "\n"),
      footer = #items .. " event(s)",
    }
  end
  return pages
end

-- ── command handlers ─────────────────────────────────────────────────────────
local function do_list(ctx)
  local s = arch.sigils(ctx.args)
  local owner_kind, owner_id, err = scope_for(ctx, s.group)
  if err then ctx.error(err) return end
  local items = ctx.store.query("items", {
    kind = "event", owner_kind = owner_kind, owner_id = owner_id,
  })
  local where = owner_kind == "user" and "Your events"
    or ("Group #" .. owner_id .. " events")
  ctx.deliver(
    list_pages(where, items,
      "No events here yet. Add one with `" .. ctx.prefix .. "event add`."),
    { private = owner_kind ~= "group" })
end

local function do_add(ctx)
  local s = arch.sigils(ctx.args)
  local when_raw, title_raw = s.text:match("^(.-)|(.*)$")
  if not when_raw or title_raw:gsub("%s", "") == "" then
    ctx.error("Use `event add <when> | <title>`, for example "
      .. "`event add in 2d | Team sync`.")
    return
  end
  local epoch = arch.parse_time((when_raw:gsub("^%s+", ""):gsub("%s+$", "")))
  if not epoch then ctx.error(TIME_HINT) return end
  local owner_kind, owner_id, err = scope_for(ctx, s.group)
  if err then ctx.error(err) return end
  local title, body = split_title((title_raw:gsub("^%s+", "")))
  local id = ctx.store.put("items", {
    kind = "event", owner_kind = owner_kind, owner_id = owner_id,
    title = title, body = body, due_at = epoch, shares = {},
    created_by = ctx.author_id, created_at = arch.now(),
  })
  ctx.deliver({
    title = "Event added", color = arch.colors.success,
    description = "Saved event `#" .. id .. "` for " .. arch.fmt_time(epoch)
      .. ". Add a reminder with `" .. ctx.prefix .. "event remind " .. id
      .. " <when>`.",
  }, { private = owner_kind ~= "group" })
end

local function do_show(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_event(ctx, id, false)
  if err then ctx.error(err) return end
  local fields = {}
  if item.body and item.body ~= "" then
    fields[#fields + 1] = { name = "Details", value = arch.clip(item.body, 1024) }
  end
  fields[#fields + 1] = { name = "Event time",
                          value = arch.fmt_time(item.due_at), inline = true }
  if item.remind_at then
    fields[#fields + 1] = {
      name = "Reminder",
      value = arch.fmt_time(item.remind_at)
        .. (item.reminded and " (sent)" or " (scheduled)"),
      inline = true,
    }
  end
  ctx.deliver({
    title = "Event #" .. item.id .. ": " .. arch.clip(item.title, 200),
    color = arch.colors.gold, fields = fields,
    footer = "event #" .. item.id,
  }, { private = item.owner_kind ~= "group" })
end

local function do_when(ctx)
  local id, when = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_event(ctx, id, true)
  if err then ctx.error(err) return end
  local epoch = arch.parse_time(when or "")
  if not epoch then ctx.error(TIME_HINT) return end
  item.due_at = epoch
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Rescheduled", color = arch.colors.success,
    description = "Event `#" .. item.id .. "` is now " .. arch.fmt_time(epoch)
      .. ".",
  }, { private = item.owner_kind ~= "group" })
end

local function do_remind(ctx)
  local id, when = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_event(ctx, id, true)
  if err then ctx.error(err) return end
  when = (when or ""):gsub("^%s+", ""):gsub("%s+$", "")
  local low = when:lower()
  if when == "" or low == "clear" or low == "off" or low == "none" then
    item.remind_at = nil
    item.reminded = false
    ctx.store.update("items", item.id, item)
    ctx.deliver({
      title = "Reminder cleared", color = arch.colors.success,
      description = "Cleared the reminder on event `#" .. item.id .. "`.",
    }, { private = item.owner_kind ~= "group" })
    return
  end
  local epoch = arch.parse_time(when)
  if not epoch then ctx.error(TIME_HINT) return end
  item.remind_at = epoch
  item.reminded = false
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Reminder set", color = arch.colors.success,
    description = "I will remind about event `#" .. item.id .. "` at "
      .. arch.fmt_time(epoch) .. ".",
  }, { private = item.owner_kind ~= "group" })
end

local function do_edit(ctx)
  local id, text = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_event(ctx, id, true)
  if err then ctx.error(err) return end
  if not text or text:gsub("%s", "") == "" then
    ctx.error("Give the new text after the id.")
    return
  end
  item.title, item.body = split_title(text)
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Updated", color = arch.colors.success,
    description = "Edited event `#" .. item.id .. "`.",
  }, { private = item.owner_kind ~= "group" })
end

local function do_del(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_event(ctx, id, true)
  if err then ctx.error(err) return end
  ctx.store.delete("items", item.id)
  ctx.deliver({
    title = "Deleted", color = arch.colors.success,
    description = "Removed event `#" .. item.id .. "`.",
  }, { private = item.owner_kind ~= "group" })
end

local function do_share(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_event(ctx, id, false)
  if err then ctx.error(err) return end
  if item.owner_kind ~= "user" or item.owner_id ~= ctx.author_id then
    ctx.error("You can only share your own personal events.")
    return
  end
  local target = first_mention(ctx)
  if not target then ctx.error("Mention the user to share it with.") return end
  if target.id == ctx.author_id then
    ctx.error("You already own that event.")
    return
  end
  local can_edit = ctx.args:lower():find("edit", 1, true) ~= nil
  item.shares = item.shares or {}
  local found = false
  for _, share in ipairs(item.shares) do
    if share.user == target.id then
      share.can_edit = can_edit
      found = true
    end
  end
  if not found then
    item.shares[#item.shares + 1] = { user = target.id, can_edit = can_edit }
  end
  ctx.store.update("items", item.id, item)
  local access = can_edit and "view and edit" or "view"
  arch.dm(target.id, {
    title = "An event was shared with you", color = arch.colors.info,
    description = ctx.author_name .. " shared event `#" .. item.id .. "` ("
      .. arch.clip(item.title, 120) .. ") with you (" .. access .. ").",
  })
  ctx.deliver({
    title = "Shared", color = arch.colors.success,
    description = "Event `#" .. item.id .. "` is now shared with "
      .. target.name .. " (" .. access .. ").",
  }, { private = true })
end

local function do_unshare(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_event(ctx, id, false)
  if err then ctx.error(err) return end
  if item.owner_kind ~= "user" or item.owner_id ~= ctx.author_id then
    ctx.error("You can only unshare your own events.")
    return
  end
  local target = first_mention(ctx)
  if not target then
    ctx.error("Mention the user to stop sharing with.")
    return
  end
  local kept, removed = {}, false
  for _, share in ipairs(item.shares or {}) do
    if share.user == target.id then
      removed = true
    else
      kept[#kept + 1] = share
    end
  end
  if not removed then
    ctx.error(target.name .. " did not have access to that event.")
    return
  end
  item.shares = kept
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Unshared", color = arch.colors.success,
    description = target.name .. " can no longer see event `#" .. item.id
      .. "`.",
  }, { private = true })
end

local function do_copy(ctx)
  local id, rest = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_event(ctx, id, false)
  if err then ctx.error(err) return end
  local kind, oid, derr = parse_dest(ctx, rest)
  if derr then ctx.error(derr) return end
  local new_id = ctx.store.put("items", {
    kind = "event", owner_kind = kind, owner_id = oid,
    title = item.title, body = item.body or "", due_at = item.due_at,
    shares = {}, created_by = ctx.author_id, created_at = arch.now(),
  })
  ctx.deliver({
    title = "Copied", color = arch.colors.success,
    description = "Event copied to " .. dest_label(ctx, kind, oid)
      .. " as `#" .. new_id .. "`.",
  }, { private = kind ~= "group" })
end

local function do_move(ctx)
  local id, rest = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_event(ctx, id, true)
  if err then ctx.error(err) return end
  local kind, oid, derr = parse_dest(ctx, rest)
  if derr then ctx.error(derr) return end
  if kind == item.owner_kind and oid == item.owner_id then
    ctx.error("That event is already there.")
    return
  end
  item.owner_kind, item.owner_id, item.shares = kind, oid, {}
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Moved", color = arch.colors.success,
    description = "Event `#" .. item.id .. "` moved to "
      .. dest_label(ctx, kind, oid) .. ".",
  }, { private = kind ~= "group" })
end

-- ── reminder loop ────────────────────────────────────────────────────────────
local function fire_reminders()
  local now = arch.now()
  for _, item in ipairs(arch.store.query("items", { kind = "event" })) do
    if item.remind_at and not item.reminded and item.remind_at <= now then
      local recipients
      if item.owner_kind == "group" then
        local grp = arch.store.get("groups", item.owner_id)
        recipients = grp and (grp.members or {}) or {}
      else
        recipients = { item.owner_id }
      end
      local fields = {}
      if item.body and item.body ~= "" then
        fields[#fields + 1] = { name = "Details", value = arch.clip(item.body, 1024) }
      end
      if item.due_at then
        fields[#fields + 1] = { name = "Scheduled",
                                value = arch.fmt_time(item.due_at), inline = true }
      end
      local note = {
        title = "Event reminder", color = arch.colors.gold,
        description = "**" .. arch.clip(item.title, 240) .. "**",
        fields = fields, footer = "event #" .. item.id,
      }
      for _, uid in ipairs(recipients) do
        arch.dm(uid, note)
      end
      item.reminded = true
      arch.store.update("items", item.id, item)
    end
  end
end

-- ── command tree ─────────────────────────────────────────────────────────────
M.commands = {
  {
    name = "event", aliases = { "events", "cal", "calendar" },
    summary = "Calendar events with reminders.",
    run = do_list,
    subcommands = {
      { name = "add", aliases = { "new", "create" },
        usage = "add [#group] <when> | <title>",
        summary = "Add an event (time before the `|`).", run = do_add },
      { name = "list", aliases = { "ls", "all" }, usage = "list [#group]",
        summary = "List your events, or a group's events.", run = do_list },
      { name = "show", aliases = { "view", "open" }, usage = "show <id>",
        summary = "Open a single event.", run = do_show },
      { name = "when", aliases = { "reschedule" }, usage = "when <id> <when>",
        summary = "Reschedule an event.", run = do_when },
      { name = "remind", usage = "remind <id> <when|clear>",
        summary = "Set or clear an event reminder.", run = do_remind },
      { name = "edit", usage = "edit <id> <text>",
        summary = "Replace an event's text.", run = do_edit },
      { name = "del", aliases = { "delete", "rm", "remove" }, usage = "del <id>",
        summary = "Delete an event.", run = do_del },
      { name = "share", usage = "share <id> @user [edit]",
        summary = "Share a personal event with a user.", run = do_share },
      { name = "unshare", usage = "unshare <id> @user",
        summary = "Stop sharing an event with a user.", run = do_unshare },
      { name = "copy", usage = "copy <id> <me|@user|#group>",
        summary = "Copy an event somewhere else.", run = do_copy },
      { name = "move", usage = "move <id> <me|@user|#group>",
        summary = "Move an event somewhere else.", run = do_move },
    },
  },
}

M.loops = {
  { name = "reminders", interval = 60, run = fire_reminders },
}

return M
