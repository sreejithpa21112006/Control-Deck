package com.sreejith.controldeck;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;

/**
 * Shows what the laptop is playing in the phone's notifications and on its lock screen,
 * with previous, play or pause, and next buttons that control the laptop.
 */
class MediaControls {

    interface Listener {
        void onCommand(String name);
    }

    private static final String ACTION = "com.sreejith.controldeck.MEDIA";
    private static final String CHANNEL = "laptop_media";
    private static final int NOTE_ID = 7;

    private final Activity activity;
    private final Listener listener;
    private final NotificationManager manager;
    private final MediaSession session;
    private final BroadcastReceiver receiver;
    private boolean shown = false;

    MediaControls(Activity a, Listener l) {
        activity = a;
        listener = l;
        manager = (NotificationManager) a.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Laptop media", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            manager.createNotificationChannel(ch);
        }
        session = new MediaSession(a, "ControlDeck");
        session.setCallback(new MediaSession.Callback() {
            @Override
            public void onPlay() {
                listener.onCommand("playpause");
            }

            @Override
            public void onPause() {
                listener.onCommand("playpause");
            }

            @Override
            public void onSkipToNext() {
                listener.onCommand("next");
            }

            @Override
            public void onSkipToPrevious() {
                listener.onCommand("prev");
            }
        });
        receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                String name = i.getStringExtra("cmd");
                if (name != null) {
                    listener.onCommand(name);
                }
            }
        };
        IntentFilter filter = new IntentFilter(ACTION);
        if (Build.VERSION.SDK_INT >= 33) {
            a.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            a.registerReceiver(receiver, filter);
        }
    }

    private PendingIntent action(String cmd, int code) {
        Intent i = new Intent(ACTION);
        i.setPackage(activity.getPackageName());
        i.putExtra("cmd", cmd);
        return PendingIntent.getBroadcast(activity, code, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    @SuppressWarnings("deprecation")
    void update(String title, String artist, boolean playing, boolean on) {
        if (!on) {
            clear();
            return;
        }
        session.setActive(true);
        session.setMetadata(new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, title)
                .putString(MediaMetadata.METADATA_KEY_ARTIST, artist)
                .build());
        long actions = PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE | PlaybackState.ACTION_PLAY_PAUSE
                | PlaybackState.ACTION_SKIP_TO_NEXT | PlaybackState.ACTION_SKIP_TO_PREVIOUS;
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(actions)
                .setState(playing ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED,
                        PlaybackState.PLAYBACK_POSITION_UNKNOWN, playing ? 1f : 0f)
                .build());

        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(activity, CHANNEL)
                : new Notification.Builder(activity);
        b.setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle(title)
                .setContentText(artist.length() > 0 ? artist : "On your laptop")
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .addAction(android.R.drawable.ic_media_previous, "Previous", action("prev", 1))
                .addAction(playing ? android.R.drawable.ic_media_pause : android.R.drawable.ic_media_play,
                        playing ? "Pause" : "Play", action("playpause", 2))
                .addAction(android.R.drawable.ic_media_next, "Next", action("next", 3))
                .setStyle(new Notification.MediaStyle()
                        .setMediaSession(session.getSessionToken())
                        .setShowActionsInCompactView(0, 1, 2));
        Intent open = activity.getPackageManager().getLaunchIntentForPackage(activity.getPackageName());
        if (open != null) {
            b.setContentIntent(PendingIntent.getActivity(activity, 0, open, PendingIntent.FLAG_IMMUTABLE));
        }
        manager.notify(NOTE_ID, b.build());
        shown = true;
    }

    void clear() {
        if (shown) {
            manager.cancel(NOTE_ID);
            shown = false;
        }
        session.setActive(false);
    }

    void release() {
        clear();
        try {
            activity.unregisterReceiver(receiver);
        } catch (RuntimeException ignored) {
            // already gone
        }
        session.release();
    }
}
