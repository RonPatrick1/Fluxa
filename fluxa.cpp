#include <gtk/gtk.h>

// Function prototypes
static void on_button_clicked(GtkWidget *widget, gpointer data);
static void activate(GtkApplication* app, gpointer user_data);

// Callback function for the button click event
static void on_button_clicked(GtkWidget *widget, gpointer data) {
    GtkWidget *dialog;

    dialog = gtk_message_dialog_new(
        NULL,
        GTK_DIALOG_MODAL,
        GTK_MESSAGE_INFO,
        GTK_BUTTONS_OK,
        "Hello Elizabeth and Frederick!"
    );

    gtk_widget_show_all(dialog);

    g_signal_connect(dialog, "response", G_CALLBACK(gtk_widget_destroy), NULL);
}

// Main activation function for the application
static void activate(GtkApplication* app, gpointer user_data) {
    GtkWidget *window;
    GtkWidget *box;
    GtkWidget *label;
    GtkWidget *button;

    // Create a new window
    window = gtk_application_window_new(app);
    gtk_window_set_title(GTK_WINDOW(window), "Hello Ron");
    gtk_window_set_default_size(GTK_WINDOW(window), 300, 200);

    // Create a vertical box to hold the widgets
    box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 10);
    gtk_container_add(GTK_CONTAINER(window), box);

    // Create a label
    label = gtk_label_new("Hello Ron! Welcome to your GTK3 Application.");
    gtk_box_pack_start(GTK_BOX(box), label, TRUE, TRUE, 0);

    // Create a button
    button = gtk_button_new_with_label("Click Me!");
    g_signal_connect(button, "clicked", G_CALLBACK(on_button_clicked), NULL);
    gtk_box_pack_start(GTK_BOX(box), button, FALSE, FALSE, 0);

    // Show all widgets
    gtk_widget_show_all(window);
}

// Main function
int main(int argc, char **argv) {
    GtkApplication *app;
    int status;

    app = gtk_application_new("org.gtk.example", G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(app, "activate", G_CALLBACK(activate), NULL);
    status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);

    return status;
}
