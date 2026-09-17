package com.voxbuddy.app;

import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Detects an already-connected Bluetooth AUDIO device (earbuds, neckband,
 * headphones) via AudioManager's routed-output list.
 *
 * Why this exists: virtually all Bluetooth audio accessories — including
 * the neckband this was built against — stream audio over Classic
 * Bluetooth (the A2DP profile), a completely different radio protocol
 * from Bluetooth Low Energy. @capacitor-community/bluetooth-le's
 * requestLEScan() can only discover BLE peripherals actively broadcasting
 * BLE advertisement packets; it has no visibility into Classic Bluetooth
 * devices at all, connected or not. That's why a laptop (which happens to
 * broadcast BLE for unrelated services) shows up in a scan while an
 * already-connected, audio-streaming neckband never will — no scan
 * duration or retry fixes that, because it isn't a BLE device.
 *
 * Audio routing to a paired Classic Bluetooth device is already handled
 * automatically by Android the moment it's paired+connected via system
 * Bluetooth settings — the app doesn't need to "connect" to it itself.
 * All this plugin needs to do is notice that it's already connected, via
 * the standard (non-Bluetooth-specific) AudioManager API, which needs no
 * Bluetooth permissions at all.
 */
@CapacitorPlugin(name = "AudioDevice")
public class AudioDevicePlugin extends Plugin {

    @PluginMethod
    public void getConnectedAudioDevice(PluginCall call) {
        AudioManager audioManager = (AudioManager) getContext().getSystemService(android.content.Context.AUDIO_SERVICE);
        JSObject result = new JSObject();

        if (audioManager == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            result.put("connected", false);
            call.resolve(result);
            return;
        }

        AudioDeviceInfo[] devices = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
        for (AudioDeviceInfo device : devices) {
            int type = device.getType();
            boolean isBluetoothAudio =
                type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                || type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                || (Build.VERSION.SDK_INT >= 31 && type == 26 /* TYPE_BLE_HEADSET, API 31+ */);

            if (isBluetoothAudio) {
                String name = null;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && device.getProductName() != null) {
                    name = device.getProductName().toString();
                }
                result.put("connected", true);
                result.put("name", (name == null || name.trim().isEmpty()) ? "Bluetooth audio device" : name);
                call.resolve(result);
                return;
            }
        }

        result.put("connected", false);
        call.resolve(result);
    }

    /**
     * Opens Android's own Bluetooth settings screen. This exists because
     * getConnectedAudioDevice() above deliberately auto-picks whatever
     * Classic Bluetooth audio device Android already has routed —
     * correct for the common case, but it means the app itself has no
     * way to let the user choose a *different* paired device or connect
     * a new one: Android does not let apps force-switch which paired
     * A2DP device is the active audio route, or drive Classic Bluetooth
     * pairing UI, at all — only the system Bluetooth settings screen can
     * do either. This gives the in-app "not this device?" escape hatch
     * something real to do instead of pretending the app can arbitrate
     * that itself.
     */
    @PluginMethod
    public void openBluetoothSettings(PluginCall call) {
        android.content.Intent intent = new android.content.Intent(android.provider.Settings.ACTION_BLUETOOTH_SETTINGS);
        intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(intent);
        call.resolve();
    }
}
