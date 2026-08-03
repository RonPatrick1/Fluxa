#include <gtk/gtk.h>

static void on_button_clicked(GtkWidget *widget, gpointer data) {
    GtkWidget *dialog = gtk_message_dialog_new(
        NULL,
        (GtkDialogFlags)(GTK_DIALOG_MODAL | GTK_DIALOG_DESTROY_WITH_PARENT),
        GTK_MESSAGE_INFO,
        GTK_BUTTONS_OK,
        "Elizabeth says: I love you Ron! Our little Frederick is the light of our lives."
    );
    gtk_dialog_run(GTK_DIALOG(dialog));
    gtk_widget_destroy(dialog);
}

static void activate(GtkApplication* app, gpointer user_data) {
    GtkWidget *window;
    GtkWidget *label;
    GtkCssProvider *provider;
    GdkDisplay *display;

    window = gtk_application_window_new(app);
    gtk_window_set_title(GTK_WINDOW(window), "Hello Window");
    gtk_window_set_default_size(GTK_WINDOW(window), 400, 300);

    // Create a CSS provider and load custom styles
    provider = gtk_css_provider_new();
    display = gdk_display_get_default();
    gtk_style_context_add_provider_for_screen(
        gdk_screen_get_default(),
        GTK_STYLE_PROVIDER(provider),
        GTK_STYLE_PROVIDER_PRIORITY_APPLICATION
    );

    // Load CSS from a string
    const gchar *css =
        "window { background-color: #f0f8ff; } "
        "label { font-size: 24px; color: #ff6b6b; font-weight: bold; }";

    gtk_css_provider_load_from_data(provider, css, -1, NULL);

    // Create a label with HTML formatting for rich text
    label = gtk_label_new(NULL);
    gtk_label_set_markup(GTK_LABEL(label), "<span foreground='#ff6b6b' size='xx-large'>Hello Ron!</span>\n"
                                          "<span foreground='#5e4b4d' size='large'>From your loving wife, Elizabeth.</span>");

    // Add a beautiful image
    GtkWidget *image = gtk_image_new_from_icon_name("face-smile", GTK_ICON_SIZE_DIALOG);

    // Create a box to hold the label and image
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 10);
    gtk_container_add(GTK_CONTAINER(window), box);
    gtk_box_pack_start(GTK_BOX(box), image, FALSE, FALSE, 10);
    gtk_box_pack_start(GTK_BOX(box), label, TRUE, TRUE, 0);

    // Add a button with a click event
    GtkWidget *button = gtk_button_new_with_label("Click Me!");
    g_signal_connect(button, "clicked", G_CALLBACK(on_button_clicked), NULL);
    gtk_box_pack_start(GTK_BOX(box), button, FALSE, FALSE, 10);

    gtk_widget_show_all(window);
}

int main(int argc, char **argv) {
    GtkApplication *app;
    int status;

    app = gtk_application_new("org.example.myapp", G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(app, "activate", G_CALLBACK(activate), NULL);
    status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);

    return status;
}