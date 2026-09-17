package com.voxbuddy.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import androidx.core.app.NotificationCompat;

/**
 * Keeps VoxBuddy's mic-capture + translation WebSocket pipeline alive
 * while a conversation is active and the app is backgrounded (screen off,
 * switched to another app, etc.).
 *
 * Without this, Android throttles a backgrounded WebView's JS execution
 * within seconds and can freeze or kill the process outright once memory
 * pressure hits — which is why "listen in the background" didn't
 * previously work at all, not because of a bug in the audio pipeline
 * itself. A foreground service with a visible notification is the
 * OS-sanctioned way to say "this app is doing an active, user-requested
 * task and needs to keep running" — the same mechanism music players and
 * navigation apps use. There's no way to do this invisibly; Android
 * requires the notification specifically so a background mic can never
 * be silently active without the user's knowledge.
 *
 * This service does not touch the microphone itself — actual audio
 * capture (getUserMedia -> WebSocket) stays entirely inside the WebView's
 * JS, exactly as it already worked in the foreground. All this adds is
 * "stay alive and don't get deprioritized while that's happening,"
 * plus a partial wake lock so the CPU doesn't sleep mid-conversation.
 */
public class ConversationForegroundService extends Service {

    private static final String CHANNEL_ID = "voxbuddy_conversation";
    private static final int NOTIFICATION_ID = 4201;

    private PowerManager.WakeLock wakeLock;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIFICATION_ID, buildNotification());

        // Wrapped defensively: an uncaught exception inside a Service
        // lifecycle callback is fatal to the whole app process, not just
        // this feature (this is exactly what force-closed the app when
        // WAKE_LOCK was missing from the manifest — acquire() threw, and
        // that took the entire process down with it, not just the
        // background-listening feature). The service call above,
        // startForeground(), is the one part that must not fail
        // silently — an active foreground service without a real
        // notification would violate Android's own requirement — so only
        // the wake lock, which is a "nice to have" against CPU sleep, is
        // allowed to fail quietly here.
        try {
            if (wakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                if (pm != null) {
                    wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "VoxBuddy:conversation");
                    wakeLock.setReferenceCounted(false);
                }
            }
            if (wakeLock != null && !wakeLock.isHeld()) {
                // No timeout here deliberately — released explicitly in
                // onDestroy() when JS calls ConversationService.stop(), not
                // left to expire mid-conversation.
                wakeLock.acquire();
            }
        } catch (Exception e) {
            // Background listening simply won't resist CPU sleep as
            // aggressively on whatever device hits this — the
            // conversation itself still works fine either way.
        }

        // START_STICKY: if the OS still kills this process under severe
        // memory pressure despite the foreground priority, ask it to
        // restart the service (though the WebView/JS state itself won't
        // survive a full process kill — this covers the common
        // "deprioritized, not killed" case, which is the vast majority
        // of real background-listening interruptions).
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private Notification buildNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager manager = getSystemService(NotificationManager.class);
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "Active conversation", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Shown while VoxBuddy is listening and translating in the background.");
            if (manager != null) manager.createNotificationChannel(channel);
        }

        Intent tapIntent = new Intent(this, MainActivity.class);
        tapIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
            | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent pendingIntent = PendingIntent.getActivity(this, 0, tapIntent, flags);

        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("VoxBuddy is listening")
            .setContentText("Translating your conversation in real time.")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .build();
    }
}
