-- ============================================================================
-- evolab_driver — EvoLab 进化竞技场自动驾驶模块（AoE2Control）
-- ============================================================================
-- 用法：把本模块指派给【玩家 1】（CONTROL 菜单 → MODULES → Player 1）。
--
-- 工作流程：
--   Load()  游戏主菜单里自动配置对局并开局（3 玩家：P1 人类槽位 + P2/P3 两个 AI）
--   Init()  开局后让 P1 自毁 → 你变成观察者，两个 .per AI 互相对战
--   End()   对局结束 → 立即自动开下一局（同一房间配置）
--
-- 对局双方 AI = 游戏 AI 目录下的 EvoAI_A.per / EvoAI_B.per，
-- 内容由 Python 侧在每局间隙改写（固定槽位名，换内容不换名），
-- 因此本模块完全无感，只管开车。
--
-- 停止方法：CONTROL 菜单（Shift）把 Player 1 的模块关掉，或按 Delete 卸载 CONTROL。
-- 注意：MULTITHREADING / TOURNAMENT MODE 必须保持关闭，否则 Dispatch* 会被拒绝。
-- ============================================================================

-- 可调参数（与 ai_lab/config.json 的 control 段对应；改完在游戏里重选一次模块即生效）
local CFG = {
    map          = OptionsLocation.ARABIA,        -- 进化基准地图
    map_size     = OptionsMapSize.SMALL,          -- 3 人局用小图，节奏快
    difficulty   = OptionsAIDifficulty.HARD,      -- 必须与进化参数分支一致（*-hard）
    population   = 200,
    speed        = 2.0,                           -- 游戏速度
    players      = 3,                             -- P1 观察者 + P2/P3 对战双方
    victory      = OptionsVictory.CONQUEST,       -- 征服胜利：只剩一方即结束
    victory_time = 45,                            -- spectate_mode="stay" 时的时间限制（分钟）
    spectate_mode = "eliminate",                  -- "eliminate"=P1自毁观战；"stay"=P1挂机+限时比分
}

local RESULT_CAPTURE_SETTING = "Read-only result capture PoC"
local RESULT_CAPTURE_PREFIX = "EVOLAB_RESULT_CAPTURE_V1:"
local RESULT_CAPTURE_PIPE = "EvoLabResultCaptureV1"
local RESULT_CAPTURE_MODULE_BUILD = "sour100-ipc-update-poll-v2"
local capture_enabled = false
local capture_ipc_started = { ok = false, error = "PoC not enabled" }
local capture_match_id = nil
local capture_sequence = 0
local capture_ready_logged = false
local captured_game_speed_set = { ok = false, error = "PoC not enabled" }
local captured_game_speed_readback = { ok = false, error = "PoC not enabled" }
local capture_update_count = 0
local capture_hello_count = 0

local function capture_value(callback)
    local ok, value = pcall(callback)
    if not ok then
        return { ok = false, error = tostring(value) }
    end
    if value == nil then
        return { ok = true, value_type = "nil" }
    end
    return { ok = true, value_type = type(value), value = value }
end

local function capture_call(callback)
    local ok, value = pcall(callback)
    if not ok then
        return { call_ok = false, error = tostring(value), return_value = { ok = false } }
    end
    return {
        call_ok = true,
        return_value = value == nil and { ok = true, value_type = "nil" }
            or { ok = true, value_type = type(value), value = value },
    }
end

local function capture_player(slot)
    local ok, player = pcall(GetPlayerById, slot)
    if not ok or player == nil then
        return {
            slot = slot,
            player = { ok = ok, value_type = "nil", error = ok and nil or tostring(player) },
            has_won = { ok = false, error = "player unavailable" },
            current_score = { ok = false, error = "player unavailable" },
        }
    end
    local name = capture_value(function() return player:GetPlayerName() end)
    return {
        slot = slot,
        name = name.ok and name.value or nil,
        has_won = capture_value(function() return player:HasWon() end),
        current_score = capture_value(function() return player:GetFact(Fact.CURRENT_SCORE) end),
    }
end

local function capture_victory_player()
    local ok, player = pcall(GetVictoryPlayer)
    if not ok then
        return { ok = false, value_type = "error", error = tostring(player) }
    end
    if player == nil then
        return { ok = true, value_type = "nil" }
    end
    local id = capture_value(function() return player:GetId() end)
    local name = capture_value(function() return player:GetPlayerName() end)
    return {
        ok = true,
        value_type = "Player",
        player_id = id.value,
        player_name = name.value,
        player_id_raw = id,
        player_name_raw = name,
    }
end

local function start_capture_ipc_server(lifecycle_stage)
    if not capture_enabled then
        Log("EvoLab PoC IPC server skipped: " .. tostring(lifecycle_stage) .. " setting=false")
        return
    end
    local ok, started = pcall(function() return IPC.StartServer(RESULT_CAPTURE_PIPE) end)
    local start_error = nil
    if not ok then
        start_error = tostring(started)
    elseif started ~= true then
        start_error = "IPC.StartServer returned false"
    end
    capture_ipc_started = {
        call_ok = ok,
        started = started == true,
        ok = ok and started == true,
        error = start_error,
    }
    Log("EvoLab PoC IPC StartServer result: " .. ToJSON({
        module_build = RESULT_CAPTURE_MODULE_BUILD,
        lifecycle_stage = lifecycle_stage,
        pipe = RESULT_CAPTURE_PIPE,
        call_ok = capture_ipc_started.call_ok,
        server_started = capture_ipc_started.started,
        error = capture_ipc_started.error,
    }))
end

function Load(playerId)
    Settings.AddBool("AutoDrive", true)
    Settings.AddBool(RESULT_CAPTURE_SETTING, false)
    if playerId ~= 1 then
        return  -- 本模块只由玩家 1 的槽位驱动，其他槽位误挂时静默退出
    end
    capture_enabled = Settings.GetBool(RESULT_CAPTURE_SETTING, false)
    Log("EvoLab PoC lifecycle Load: " .. ToJSON({
        module_build = RESULT_CAPTURE_MODULE_BUILD,
        assigned_player_id = playerId,
        capture_enabled = capture_enabled,
        auto_drive = Settings.GetBool("AutoDrive", true),
    }))
    start_capture_ipc_server("Load")
    if not Settings.GetBool("AutoDrive", true) then
        Log("EvoLab: AutoDrive 已关闭，待机。")
        return
    end

    local options = GetCurrentGameOptions()
    if not options then
        Log("EvoLab: 当前没有可用对局配置")
        return
    end

    options:SetGameMode(OptionsGameMode.RANDOM_MAP)
    options:SetLocation(CFG.map)
    options:SetMapSize(CFG.map_size)
    options:SetAIDifficulty(CFG.difficulty)
    options:SetPopulation(CFG.population)
    options:SetResources(OptionsResources.STANDARD)
    if CFG.spectate_mode == "stay" then
        options:SetVictory(OptionsVictory.TIME_LIMIT)
        options:SetVictoryLimit(CFG.victory_time)
    else
        options:SetVictory(CFG.victory)
    end
    options:SetRecordGame(true)      -- 必须开录像：Python 战报解析依赖 .aoe2record
    if capture_enabled then
        captured_game_speed_set = capture_call(function() return options:SetGameSpeed(CFG.speed) end)
        captured_game_speed_readback = capture_value(function() return options:GetGameSpeed() end)
        Log("EvoLab PoC speed set/readback: " .. ToJSON({
            requested = CFG.speed,
            set = captured_game_speed_set,
            readback = captured_game_speed_readback,
        }))
    else
        options:SetGameSpeed(CFG.speed)
    end
    options:SetLockSpeed(true)
    options:SetPlayersCount(CFG.players)
    -- 三方各自为战
    for i = 0, CFG.players - 1 do
        options:SetPlayerTeam(i, i + 1)
    end

    Log("EvoLab: 配置完成，自动开局（P1=观察者, P2/P3=EvoAI_A/B）")
    DispatchStartGame()
end

function Init()
    Log("EvoLab PoC lifecycle Init: " .. ToJSON({
        module_build = RESULT_CAPTURE_MODULE_BUILD,
        assigned_player_id = GetAssignedPlayerId(),
        capture_enabled = capture_enabled,
    }))
    if GetAssignedPlayerId() ~= 1 then
        return
    end
    if CFG.spectate_mode ~= "eliminate" then
        return
    end
    -- 删除自己全部单位与建筑，成为旁观者，把舞台让给两个 AI
    local mine = GetObjectsInArea(Vector2.new(0, 0), Vector2.new(480, 480), nil, GetAssignedPlayerId())
    local n = 0
    for _, obj in ipairs(mine) do
        local ok = pcall(DeleteUnit, obj)
        if ok then n = n + 1 end
    end
    Log("EvoLab: 观察模式，已移除己方对象 " .. tostring(n) .. " 个")
end

function Update()
    if not capture_enabled or not capture_ipc_started.ok then
        return
    end
    capture_update_count = capture_update_count + 1
    if capture_update_count == 1 then
        Log("EvoLab PoC IPC Update polling started: " .. ToJSON({
            module_build = RESULT_CAPTURE_MODULE_BUILD,
            update_count = capture_update_count,
            server_started = capture_ipc_started.ok,
        }))
    end
    local ok, messages = pcall(function() return IPC.GetMessages() end)
    if not ok or type(messages) ~= "table" then
        Log("EvoLab PoC IPC GetMessages failed: " .. tostring(messages))
        return
    end
    for _, raw in ipairs(messages) do
        local parsed = ParseJSON(raw)
        if type(parsed) == "table" and parsed.action == "capture_hello"
            and parsed.protocol_version == 1 then
            capture_hello_count = capture_hello_count + 1
            local ready_ok, queued = pcall(function()
                return IPC.Send({ action = "capture_ready", protocol_version = 1,
                    module_build = RESULT_CAPTURE_MODULE_BUILD })
            end)
            if ready_ok and queued == true then
                if not capture_ready_logged or capture_hello_count % 10 == 0 then
                    Log("EvoLab PoC IPC capture_ready queued after client hello: " .. ToJSON({
                        module_build = RESULT_CAPTURE_MODULE_BUILD,
                        hello_count = capture_hello_count,
                        update_count = capture_update_count,
                    }))
                    capture_ready_logged = true
                end
            else
                Log("EvoLab PoC IPC capture_ready send failed: " .. ToJSON({
                    module_build = RESULT_CAPTURE_MODULE_BUILD,
                    hello_count = capture_hello_count,
                    update_count = capture_update_count,
                    error = tostring(queued),
                }))
            end
        elseif type(parsed) == "table" and parsed.action == "bind_match"
            and type(parsed.match_id) == "string" and parsed.match_id ~= ""
            and (capture_match_id == nil or capture_match_id == parsed.match_id) then
            local sent_ok, queued = pcall(function()
                return IPC.Send({ action = "match_bound", match_id = parsed.match_id })
            end)
            if sent_ok and queued == true then
                capture_match_id = parsed.match_id
                Log("EvoLab PoC runner match_id bound: " .. capture_match_id)
            else
                Log("EvoLab PoC IPC binding acknowledgement failed: " .. tostring(queued))
            end
        else
            Log("EvoLab PoC IPC binding rejected")
        end
    end
end

function End(hasWon)
    if GetAssignedPlayerId() ~= 1 then
        return
    end
    if not Settings.GetBool("AutoDrive", true) then
        return
    end
    if capture_enabled then
        capture_sequence = capture_sequence + 1
        local observation = {
            schema_version = 1,
            capture_sequence = capture_sequence,
            match_id = capture_match_id,
            callback_has_won = capture_value(function() return hasWon end),
            game_time_seconds = capture_value(GetGameTime),
            game_speed_set = captured_game_speed_set,
            game_speed_readback = captured_game_speed_readback,
            victory_player = capture_victory_player(),
            players = { capture_player(2), capture_player(3) },
        }
        local raw_sentinel = RESULT_CAPTURE_PREFIX .. ToJSON(observation)
        Log(raw_sentinel)
        if capture_ipc_started.ok then
            local sent_ok, queued = pcall(function() return IPC.Send(raw_sentinel) end)
            Log("EvoLab PoC IPC send: " .. ToJSON({ call_ok = sent_ok, queued = queued }))
        else
            Log("EvoLab PoC IPC unavailable; raw sentinel was not delivered: " ..
                tostring(capture_ipc_started.error))
        end
        Log("EvoLab PoC 已采集本局原始终局字段；本次未自动启动下一局。")
        return
    end
    Log("EvoLab: 本局结束，自动开下一局")
    DispatchStartGame()
end
