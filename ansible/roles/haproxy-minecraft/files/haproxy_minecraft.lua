-- Decode the initial Minecraft Java Edition handshake for HAProxy routing.
-- Derived from Nathan Poirier's MIT-licensed implementation:
-- https://gist.github.com/nathan818fr/a078e92604784ad56e84843ebf99e2e5

local byte = string.byte
local sub = string.sub
local find = string.find
local len = string.len

local function readable(payload)
    return len(payload.data) - payload.pos + 1
end

-- Returns: value, status where status is "ok", "wait", or "invalid".
local function read_varint(payload, max_bytes)
    local value = 0
    local count = 0

    while count < max_bytes do
        local b = byte(payload.data, payload.pos + count)
        if b == nil then
            return nil, "wait"
        end

        value = value | ((b & 0x7f) << (7 * count))
        count = count + 1

        if b < 0x80 then
            payload.pos = payload.pos + count
            return value, "ok"
        end
    end

    return nil, "invalid"
end

local function read_string(payload, max_bytes, max_length)
    local str_length, status = read_varint(payload, max_bytes)
    if status ~= "ok" then
        return nil, status
    end
    if str_length < 0 or str_length > max_length then
        return nil, "invalid"
    end
    if readable(payload) < str_length then
        return nil, "wait"
    end

    -- string.sub's end index is inclusive.
    local value = sub(payload.data, payload.pos, payload.pos + str_length - 1)
    payload.pos = payload.pos + str_length
    return value, "ok"
end

local function decode_handshake(data)
    if data == nil or len(data) == 0 then
        return nil, "wait"
    end

    local payload = { data = data, pos = 1 }

    local packet_length, status = read_varint(payload, 3)
    if status ~= "ok" then
        return nil, status
    end
    if packet_length > 267 then
        return nil, "invalid"
    end
    if readable(payload) < packet_length then
        return nil, "wait"
    end

    local packet_id
    packet_id, status = read_varint(payload, 1)
    if status ~= "ok" or packet_id ~= 0 then
        return nil, "invalid"
    end

    local protocol_version
    protocol_version, status = read_varint(payload, 5)
    if status ~= "ok" then
        return nil, status
    end

    local hostname
    hostname, status = read_string(payload, 2, 255)
    if status ~= "ok" then
        return nil, status
    end

    if readable(payload) < 2 then
        return nil, "wait"
    end
    payload.pos = payload.pos + 2 -- unsigned short: requested port

    local next_state
    next_state, status = read_varint(payload, 1)
    if status ~= "ok" then
        return nil, status
    end
    if next_state ~= 1 and next_state ~= 2 then
        return nil, "invalid"
    end

    -- Forge/NeoForge and some proxy clients append metadata after a NUL byte.
    local nul = find(hostname, "\0", 1, true)
    if nul ~= nil then
        hostname = sub(hostname, 1, nul - 1)
    end

    -- DNS hostnames are case-insensitive and a trailing dot is equivalent.
    hostname = string.lower(hostname):gsub("%.$", "")

    return {
        protocol_version = protocol_version,
        hostname = hostname,
        next_state = next_state,
    }, "complete"
end

local function mc_handshake(txn)
    local result, status = decode_handshake(txn.req:dup())
    txn:set_var("txn.mc_parse_status", status)

    if result ~= nil then
        txn:set_var("txn.mc_proto", result.protocol_version)
        txn:set_var("txn.mc_host", result.hostname)
        txn:set_var("txn.mc_state", result.next_state)
    end
end

core.register_action("mc_handshake", { "tcp-req" }, mc_handshake, 0)
