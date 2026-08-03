package com.liamapp.bose_battery_voice

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import java.io.IOException
import java.util.UUID

object BoseBatteryReader {
    private val SERIAL_PORT_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

    @SuppressLint("MissingPermission")
    fun read(device: BluetoothDevice): Int {
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

    private fun readConnected(socket: BluetoothSocket): Int {
        val input = socket.inputStream
        val output = socket.outputStream

        output.write(BmapCodec.encode(0x00, 0x01, 0x01))
        output.flush()
        receiveExpected(input, 0x00, 0x01)

        output.write(BmapCodec.encode(0x02, 0x02, 0x01))
        output.flush()
        val battery = receiveExpected(input, 0x02, 0x02)
        if (battery.payload.isEmpty()) throw IOException("Bose returned no battery level")
        return battery.payload[0].toInt() and 0xff
    }

    private fun receiveExpected(
        input: java.io.InputStream,
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
}
