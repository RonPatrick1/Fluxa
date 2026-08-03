package com.liamapp.bose_battery_voice

import java.io.EOFException
import java.io.InputStream

data class BmapPacket(
    val block: Int,
    val function: Int,
    val operator: Int,
    val payload: ByteArray,
)

object BmapCodec {
    fun encode(block: Int, function: Int, operator: Int, payload: ByteArray = byteArrayOf()): ByteArray {
        require(payload.size <= 255)
        return byteArrayOf(
            block.toByte(),
            function.toByte(),
            operator.toByte(),
            payload.size.toByte(),
        ) + payload
    }

    fun read(input: InputStream): BmapPacket {
        val header = readExactly(input, 4)
        val size = header[3].toInt() and 0xff
        return BmapPacket(
            block = header[0].toInt() and 0xff,
            function = header[1].toInt() and 0xff,
            operator = header[2].toInt() and 0x0f,
            payload = readExactly(input, size),
        )
    }

    private fun readExactly(input: InputStream, count: Int): ByteArray {
        val data = ByteArray(count)
        var offset = 0
        while (offset < count) {
            val read = input.read(data, offset, count - offset)
            if (read < 0) throw EOFException("Bose closed the Bluetooth connection")
            offset += read
        }
        return data
    }
}
