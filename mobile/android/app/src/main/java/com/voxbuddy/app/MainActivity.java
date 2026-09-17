package com.voxbuddy.app;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Registers native plugins — must happen before super.onCreate()
        // sets up the Capacitor bridge. AudioDevicePlugin: detects an
        // already-connected Classic Bluetooth audio device.
        // ConversationServicePlugin: keeps mic capture alive in the
        // background during an active conversation.
        registerPlugin(AudioDevicePlugin.class);
        registerPlugin(ConversationServicePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
