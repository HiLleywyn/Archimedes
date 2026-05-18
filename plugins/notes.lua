-- notes.lua -- the Notes plugin for Archimedes.
--
-- Private and shareable notes backed by the shared `productivity` document
-- store. A personal note lives in your DMs and follows you across servers; a
-- note filed in a group is shared with every member and answered in channel.
--
-- This file is a standalone plugin: it registers the `.note` command group
-- and nothing else. Notes, tasks, events and groups all read and write the
-- same `productivity` storage namespace, so they interoperate cleanly.

local PAGE = 10

local M = {}

M.manifest = {
  id = "notes",
  name = "Notes",
  version = "1.0.0",
  description = "Private and shared notes, with sharing and groups.",
  author = "HiLleywyn",
  category = "Productivity",
  storage = "productivity",
}

-- ── helpers ──────────────────────────────────────────────────────────────────
local function is_member(grp, user_id)
  for _, uid in ipairs(grp.members or {}) do
    if uid == user_id then return true end
  end
  return false
end

local function scope_for(ctx, group_id)
  -- Resolve a `#group` sigil to (owner_kind, owner_id) or an error string.
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

local function require_note(ctx, item_id, need_edit)
  -- Fetch a note the caller may touch, or return (nil, false, error).
  if not item_id then
    return nil, false, "Give the note id."
  end
  local item = ctx.store.get("items", item_id)
  if not item or item.kind ~= "note" then
    return nil, false, "There is no note #" .. tostring(item_id) .. "."
  end
  if item.owner_kind == "group" then
    local grp = ctx.store.get("groups", item.owner_id)
    if not grp or not is_member(grp, ctx.author_id) then
      return nil, false, "That note belongs to a group you are not in."
    end
    return item, true
  end
  if item.owner_id == ctx.author_id then
    return item, true
  end
  for _, share in ipairs(item.shares or {}) do
    if share.user == ctx.author_id then
      if need_edit and not share.can_edit then
        return nil, false, "That note is shared with you as view-only."
      end
      return item, share.can_edit and true or false
    end
  end
  return nil, false, "You do not have access to that note."
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

local function owner_label(ctx, item)
  if item.owner_kind == "group" then
    local grp = ctx.store.get("groups", item.owner_id)
    if grp then
      return "group " .. grp.name .. " (#" .. item.owner_id .. ")"
    end
    return "group #" .. item.owner_id
  end
  if item.owner_id == ctx.author_id then return "you" end
  return "shared with you"
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

local function list_pages(title, items, empty)
  if #items == 0 then
    return { { title = title, description = empty, color = arch.colors.neutral } }
  end
  local pages = {}
  for start = 1, #items, PAGE do
    local lines = {}
    for i = start, math.min(start + PAGE - 1, #items) do
      lines[#lines + 1] = "`#" .. items[i].id .. "` "
        .. arch.clip(items[i].title, 80)
    end
    pages[#pages + 1] = {
      title = title, color = arch.colors.info,
      description = table.concat(lines, "\n"),
      footer = #items .. " note(s)",
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
    kind = "note", owner_kind = owner_kind, owner_id = owner_id,
  })
  local where = owner_kind == "user" and "Your notes"
    or ("Group #" .. owner_id .. " notes")
  ctx.deliver(
    list_pages(where, items,
      "No notes here yet. Add one with `" .. ctx.prefix .. "note add`."),
    { private = owner_kind ~= "group" })
end

local function do_add(ctx)
  local s = arch.sigils(ctx.args)
  if s.text == "" then
    ctx.error("Give the note text. The first line becomes the title.")
    return
  end
  local owner_kind, owner_id, err = scope_for(ctx, s.group)
  if err then ctx.error(err) return end
  local title, body = split_title(s.text)
  local id = ctx.store.put("items", {
    kind = "note", owner_kind = owner_kind, owner_id = owner_id,
    title = title, body = body, shares = {},
    created_by = ctx.author_id, created_at = arch.now(),
  })
  ctx.deliver({
    title = "Note added", color = arch.colors.success,
    description = "Saved as note `#" .. id .. "`.",
  }, { private = owner_kind ~= "group" })
end

local function do_show(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_note(ctx, id, false)
  if err then ctx.error(err) return end
  local fields = {}
  if item.body and item.body ~= "" then
    fields[#fields + 1] = { name = "Details", value = arch.clip(item.body, 1024) }
  end
  fields[#fields + 1] = { name = "Owner", value = owner_label(ctx, item),
                          inline = true }
  ctx.deliver({
    title = "Note #" .. item.id .. ": " .. arch.clip(item.title, 200),
    color = arch.colors.info, fields = fields,
    footer = "note #" .. item.id,
  }, { private = item.owner_kind ~= "group" })
end

local function do_edit(ctx)
  local id, text = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_note(ctx, id, true)
  if err then ctx.error(err) return end
  if not text or text:gsub("%s", "") == "" then
    ctx.error("Give the new text after the id.")
    return
  end
  item.title, item.body = split_title(text)
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Updated", color = arch.colors.success,
    description = "Edited note `#" .. item.id .. "`.",
  }, { private = item.owner_kind ~= "group" })
end

local function do_del(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_note(ctx, id, true)
  if err then ctx.error(err) return end
  ctx.store.delete("items", item.id)
  ctx.deliver({
    title = "Deleted", color = arch.colors.success,
    description = "Removed note `#" .. item.id .. "`.",
  }, { private = item.owner_kind ~= "group" })
end

local function do_share(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_note(ctx, id, false)
  if err then ctx.error(err) return end
  if item.owner_kind ~= "user" or item.owner_id ~= ctx.author_id then
    ctx.error("You can only share your own personal notes. Group notes are "
      .. "already shared with every member.")
    return
  end
  local target = first_mention(ctx)
  if not target then ctx.error("Mention the user to share it with.") return end
  if target.id == ctx.author_id then
    ctx.error("You already own that note.")
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
    title = "A note was shared with you", color = arch.colors.info,
    description = ctx.author_name .. " shared note `#" .. item.id .. "` ("
      .. arch.clip(item.title, 120) .. ") with you (" .. access .. ").",
  })
  ctx.deliver({
    title = "Shared", color = arch.colors.success,
    description = "Note `#" .. item.id .. "` is now shared with "
      .. target.name .. " (" .. access .. ").",
  }, { private = true })
end

local function do_unshare(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_note(ctx, id, false)
  if err then ctx.error(err) return end
  if item.owner_kind ~= "user" or item.owner_id ~= ctx.author_id then
    ctx.error("You can only unshare your own notes.")
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
    ctx.error(target.name .. " did not have access to that note.")
    return
  end
  item.shares = kept
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Unshared", color = arch.colors.success,
    description = target.name .. " can no longer see note `#" .. item.id .. "`.",
  }, { private = true })
end

local function do_copy(ctx)
  local id, rest = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_note(ctx, id, false)
  if err then ctx.error(err) return end
  local kind, oid, derr = parse_dest(ctx, rest)
  if derr then ctx.error(derr) return end
  local new_id = ctx.store.put("items", {
    kind = "note", owner_kind = kind, owner_id = oid,
    title = item.title, body = item.body or "", shares = {},
    created_by = ctx.author_id, created_at = arch.now(),
  })
  ctx.deliver({
    title = "Copied", color = arch.colors.success,
    description = "Note copied to " .. dest_label(ctx, kind, oid)
      .. " as `#" .. new_id .. "`.",
  }, { private = kind ~= "group" })
end

local function do_move(ctx)
  local id, rest = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_note(ctx, id, true)
  if err then ctx.error(err) return end
  local kind, oid, derr = parse_dest(ctx, rest)
  if derr then ctx.error(derr) return end
  if kind == item.owner_kind and oid == item.owner_id then
    ctx.error("That note is already there.")
    return
  end
  item.owner_kind, item.owner_id, item.shares = kind, oid, {}
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Moved", color = arch.colors.success,
    description = "Note `#" .. item.id .. "` moved to "
      .. dest_label(ctx, kind, oid) .. ".",
  }, { private = kind ~= "group" })
end

-- ── command tree ─────────────────────────────────────────────────────────────
M.commands = {
  {
    name = "note", aliases = { "notes" },
    summary = "Private and shared notes.",
    run = do_list,
    subcommands = {
      { name = "add", aliases = { "new", "create" }, usage = "add [#group] <text>",
        summary = "Add a note (first line is the title).", run = do_add },
      { name = "list", aliases = { "ls", "all" }, usage = "list [#group]",
        summary = "List your notes, or a group's notes.", run = do_list },
      { name = "show", aliases = { "view", "open" }, usage = "show <id>",
        summary = "Open a single note.", run = do_show },
      { name = "edit", usage = "edit <id> <text>",
        summary = "Replace a note's text.", run = do_edit },
      { name = "del", aliases = { "delete", "rm", "remove" }, usage = "del <id>",
        summary = "Delete a note.", run = do_del },
      { name = "share", usage = "share <id> @user [edit]",
        summary = "Share a personal note with a user.", run = do_share },
      { name = "unshare", usage = "unshare <id> @user",
        summary = "Stop sharing a note with a user.", run = do_unshare },
      { name = "copy", usage = "copy <id> <me|@user|#group>",
        summary = "Copy a note somewhere else.", run = do_copy },
      { name = "move", usage = "move <id> <me|@user|#group>",
        summary = "Move a note somewhere else.", run = do_move },
    },
  },
}

return M
