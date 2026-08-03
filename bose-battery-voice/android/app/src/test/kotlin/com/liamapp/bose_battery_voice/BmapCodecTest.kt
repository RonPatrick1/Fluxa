package com.liamapp.bose_battery_voice

import java.io.ByteArrayInputStream
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class BmapCodecTest {
    @Test
    fun `encodes the read-only Bose battery request`() {
        assertArrayEquals(
            byteArrayOf(0x02, 0x02, 0x01, 0x00),
            BmapCodec.encode(0x02, 0x02, 0x01),
        )
    }

    @Test
    fun `parses a Bose battery response`() {
        val packet = BmapCodec.read(
            ByteArrayInputStream(byteArrayOf(0x02, 0x02, 0x03, 0x04, 0x64, 0xff.toByte(), 0xff.toByte(), 0x00)),
        )

        assertEquals(2, packet.block)
        assertEquals(2, packet.function)
        assertEquals(3, packet.operator)
        assertEquals(100, packet.payload[0].toInt() and 0xff)
    }
}
