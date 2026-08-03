package com.liamapp.bose_battery_voice

import java.io.ByteArrayInputStream
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
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

    @Test
    fun `parses every address from the Bose paired-device list`() {
        val first = byteArrayOf(1, 2, 3, 4, 5, 6)
        val second = byteArrayOf(7, 8, 9, 10, 11, 12)

        val addresses = BoseBatteryReader.parsePairedAddresses(byteArrayOf(0x03) + first + second)

        assertEquals(2, addresses.size)
        assertArrayEquals(first, addresses[0])
        assertArrayEquals(second, addresses[1])
    }

    @Test
    fun `parses current and other connected source names`() {
        val address = byteArrayOf(1, 2, 3, 4, 5, 6)
        val current = BoseBatteryReader.parseConnectedSource(
            address + byteArrayOf(0x03, 0, 0) + "Galaxy Phone".toByteArray(),
        )
        val other = BoseBatteryReader.parseConnectedSource(
            address + byteArrayOf(0x01, 0, 0) + "Ubuntu desktop".toByteArray(),
        )

        assertEquals("Galaxy Phone", current?.name)
        assertTrue(current?.isCurrentDevice == true)
        assertEquals("Ubuntu desktop", other?.name)
        assertFalse(other?.isCurrentDevice ?: true)
    }

    @Test
    fun `ignores a disconnected paired source`() {
        val payload = byteArrayOf(1, 2, 3, 4, 5, 6, 0x00, 0, 0) +
            "Old phone".toByteArray()

        assertNull(BoseBatteryReader.parseConnectedSource(payload))
    }

    @Test
    fun `joins both multipoint names and substitutes the current helper label`() {
        val phrase = BatteryVoiceSettings.connectedDevicesPhrase(
            "Ron's phone",
            listOf(
                BoseConnectedSource("Galaxy S24", isCurrentDevice = true),
                BoseConnectedSource("Ubuntu desktop", isCurrentDevice = false),
            ),
        )

        assertEquals("Ron's phone and Ubuntu desktop", phrase)
    }
}
