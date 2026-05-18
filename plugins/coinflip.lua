-- coinflip.lua -- the worked example plugin.
--
-- A complete, minimal plugin that shows every moving part: a manifest, a
-- prefix command, and an agent tool the model can call. Copy this file as a
-- starting point for your own plugin, or delete it if you do not want it.

local M = {}

M.manifest = {
  id = "coinflip",
  name = "Coin Flip",
  version = "1.0.0",
  description = "A fair coin flip, as a command and as an agent tool.",
  author = "HiLleywyn",
  category = "Fun",
}

math.randomseed(os.time())

local function flip()
  return math.random(2) == 1 and "heads" or "tails"
end

-- A prefix command: .coinflip (also .flip / .coin).
M.commands = {
  {
    name = "coinflip", aliases = { "flip", "coin" },
    summary = "Flip a fair coin.",
    run = function(ctx)
      ctx.reply({
        title = "Coin flip", color = arch.colors.gold,
        description = "It landed on **" .. flip() .. "**.",
      })
    end,
  },
}

-- An agent tool: the model can call this mid-conversation.
M.tools = {
  {
    name = "fun.coinflip",
    description = "Flip a fair coin. Use when a user asks for a coin flip "
      .. "or a random heads/tails decision.",
    parameters = { type = "object", properties = {} },
    handler = function(args)
      return { result = flip() }
    end,
  },
}

return M
