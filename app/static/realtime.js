/**
 * RUBO × COB — Live update client
 * Listens for `rubo_update` events from the server and reloads the page
 * when the current route cares about that kind of update.
 */

(function () {
    if (typeof io === "undefined") {
        console.warn("Socket.IO client not loaded — live updates disabled.");
        return;
    }

    const socket = io({
        transports: ["websocket", "polling"],
    });

    let reloadTimer = null;

    // Pages that should refresh on ANY relevant update
    const RELOAD_PAGES = [
        "/dashboard",
        "/accounts",
        "/transactions",
        "/messages",
        "/admin",
        "/notifications",
        "/statements",
        "/groups",
    ];

    socket.on("connect", () => {
        console.log("RUBO live connection established:", socket.id);
    });

    socket.on("connect_error", (error) => {
        console.error("RUBO Socket.IO error:", error.message);
    });

    socket.on("disconnect", (reason) => {
        console.log("RUBO disconnected:", reason);
    });

    socket.on("rubo_update", (update) => {
        console.log("RUBO update received:", update);

        const page = window.location.pathname;

        // Never auto-reload mid-transfer (user is filling a form)
        if (page === "/transfer") {
            return;
        }

        const isDirectChat = page.startsWith("/messages/");
        const isGroupChat = page.startsWith("/groups/");

        // Direct/group chat pages only care about messages
        const chatUpdateTypes = [
            "new_message",
            "message_sent",
            "new_group_message",
        ];

        let shouldReload = false;

        if (RELOAD_PAGES.includes(page)) {
            shouldReload = true;
        } else if (isDirectChat || isGroupChat) {
            shouldReload = chatUpdateTypes.includes(update.type);
        }

        if (!shouldReload) {
            return;
        }

        // Debounce so a burst of updates only triggers one reload
        clearTimeout(reloadTimer);
        reloadTimer = setTimeout(() => {
            window.location.reload();
        }, 350);
    });
})();
