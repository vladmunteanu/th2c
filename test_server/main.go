package main

import (
	"net/http"
	"fmt"
)

func main() {

	var srv http.Server
	srv.Addr = ":8080"

	http.HandleFunc("/", index_main)

	srv.ListenAndServeTLS("test_server/certs/localhost.cert", "test_server/certs/localhost.key")

	fmt.Println("Listening for HTTP/2 connections on localhost:8080")
}

func index_main(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")

	// allow pre-flight headers
	w.Header().Set("Access-Control-Allow-Headers", "Content-Range, Content-Disposition, Content-Type, ETag")

	r.Write(w)
}
