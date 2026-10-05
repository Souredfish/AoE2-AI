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

function Load(playerId)
    Settings.AddBool("AutoDrive", true)
    if playerId ~= 1 then
        return  -- 本模块只由玩家 1 的槽位驱动，其他槽位误挂时静默退出
    end
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
    options:SetGameSpeed(CFG.speed)
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

function End(hasWon)
    if GetAssignedPlayerId() ~= 1 then
        return
    end
    if not Settings.GetBool("AutoDrive", true) then
        return
    end
    Log("EvoLab: 本局结束，自动开下一局")
    DispatchStartGame()
end
