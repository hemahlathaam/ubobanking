(() => {
    const socket = io({
        transports: ["websocket", "polling"]
    });

    let reloadTimer = null;

    socket.on("connect", () => {
        console.log("MiniBank live connection working:", socket.id);
    });

    socket.on("connect_error", (error) => {
        console.error("MiniBank Socket.IO error:", error.message);
    });

    socket.on("disconnect", (reason) => {
        console.log("MiniBank disconnected:", reason);
    });

    socket.on("minibank_update", (update) => {
        console.log("MiniBank update received:", update);

        const page = window.location.pathname;

        if (page === "/transfer") {
            return;
        }

        const reloadPages = [
            "/dashboard",
            "/accounts",
            "/transactions",
            "/messages",
            "/admin",
            "/notifications",
            "/statements"
        ];

        const isChatPage = page.startsWith("/messages/");
        const isGroupPage = page.startsWith("/groups/");

        const shouldReload =
            reloadPages.includes(page) ||
            (
                isChatPage &&
                (
                    update.type === "new_message" ||
                        update.type === "message_sent" ||
                        update.type === "new_group_message"
                    )
                ||
                (
                    isGroupPage &&
                    update.type === "new_group_message"
                );

        if (!shouldReload) {
            return;
        }

        clearTimeout(reloadTimer);

        reloadTimer = setTimeout(() => {
            window.location.reload();
        }, 300);
    });
})();
