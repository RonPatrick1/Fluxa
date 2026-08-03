package com.liamapp.bose_battery_voice

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID

data class BoseConnectedSource(
    val name: String,
    val isCurrentDevice: Boolean,
)

data class BoseBatterySnapshot(
    val level: Int,
    val connectedSources: List<BoseConnectedSource>,
)

object BoseBatteryReader {
    private val SERIAL_PORT_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

    @SuppressLint("MissingPermission")
    fun read(device: BluetoothDevice): BoseBatterySnapshot {
        var firstFailure: Exception? = null
        val factories = listOf<() -> BluetoothSocket>(
            { device.createRfcommSocketToServiceRecord(SERIAL_PORT_UUID) },
            { device.createInsecureRfcommSocketToServiceRecord(SERIAL_PORT_UUID) },
        )
        for (factory in factories) {
            val socket = factory()
            try {
                socket.connect()
                return readConnected(socket)
            } catch (error: Exception) {
                if (firstFailure == null) firstFailure = error
            } finally {
                try {
                    socket.close()
                } catch (_: IOException) {
                }
            }
        }
        throw IOException("Could not open the Bose control connection", firstFailure)
    }

    private fun readConnected(socket: BluetoothSocket): BoseBatterySnapshot {
        val input = socket.inputStream
        val output = socket.outputStream

        output.write(BmapCodec.encode(0x00, 0x01, 0x01))
        output.flush()
        receiveExpected(input, 0x00, 0x01)

        output.write(BmapCodec.encode(0x02, 0x02, 0x01))
        output.flush()
        val battery = receiveExpected(input, 0x02, 0x02)
        if (battery.payload.isEmpty()) throw IOException("Bose returned no battery level")
        val level = battery.payload[0].toInt() and 0xff

        // Device discovery is optional. Newer SoundLink firmware exposes the
        // paired-device list and the status/name of each source, including both
        // sides of a multipoint connection. A speaker that lacks these packets
        // should still get its battery announcement.
        val connectedSources = try {
            readConnectedSources(input, output)
        } catch (_: Exception) {
            emptyList()
        }
        return BoseBatterySnapshot(level = level, connectedSources = connectedSources)
    }

    private fun readConnectedSources(
        input: InputStream,
        output: OutputStream,
    ): List<BoseConnectedSource> {
        output.write(BmapCodec.encode(0x04, 0x04, 0x01))
        output.flush()
        val paired = receiveExpected(input, 0x04, 0x04)
        val sources = mutableListOf<BoseConnectedSource>()
        for (address in parsePairedAddresses(paired.payload)) {
            val source = try {
                output.write(BmapCodec.encode(0x04, 0x05, 0x01, address))
                output.flush()
                parseConnectedSource(receiveExpected(input, 0x04, 0x05).payload)
            } catch (_: Exception) {
                null
            }
            if (source != null && sources.none { it.name.equals(source.name, ignoreCase = true) }) {
                sources += source
                if (sources.size == 2) break
            }
        }
        return sources
    }

    private fun receiveExpected(
        input: InputStream,
        block: Int,
        function: Int,
    ): BmapPacket {
        repeat(12) {
            val packet = BmapCodec.read(input)
            if (packet.operator == 0x04) {
                throw IOException("Bose protocol error: ${packet.payload.joinToString("") { "%02x".format(it) }}")
            }
            if (packet.block == block && packet.function == function && packet.operator == 0x03) {
                return packet
            }
        }
        throw IOException("Bose did not return the expected response")
    }

    internal fun parsePairedAddresses(payload: ByteArray): List<ByteArray> {
        if (payload.size < 7) return emptyList()
        return (1 until payload.size step 6)
            .filter { it + 6 <= payload.size }
            .map { payload.copyOfRange(it, it + 6) }
    }

    internal fun parseConnectedSource(payload: ByteArray): BoseConnectedSource? {
        // Six address bytes, status, two reserved bytes, then a UTF-8 name.
        if (payload.size < 10) return null
        val status = payload[6].toInt() and 0xff
        if (status != 0x01 && status != 0x03) return null
        val name = payload.copyOfRange(9, payload.size)
            .toString(Charsets.UTF_8)
            .trimEnd('\u0000')
            .trim()
        if (name.isEmpty()) return null
        return BoseConnectedSource(name = name, isCurrentDevice = status == 0x03)
    }
}
